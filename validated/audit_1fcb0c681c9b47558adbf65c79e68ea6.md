## Analysis

The external report's core broken invariant is: **fee/payment settlement happens without regard to whether the underlying work will actually be completed/validated**, so a relayer (or in the local analog, a user prepaying a relayer incentive) can end up paying for something that never materializes. In this repository, the closest live analog is not a Solidity gas-fee model but the **Snowbridge relayer tip top‑up mechanism** (`snowbridge-pallet-system-frontend` / `snowbridge-pallet-system-v2`), which was in fact added specifically to solve the "constant/insufficient relayer fee" problem described in the report (see `prdoc/stable2506/pr_8271.prdoc`: *"enables the ability to add a tip to an Inbound or Outbound message, in case the relayer reward is too low"*). A related fix (`prdoc/stable2509/pr_9746.prdoc`) shows the team already had to patch one instance of tips being irrecoverably burnt — this indicates the general failure pattern is real and has not been fully eliminated.

### Title
Unconditional tip burn before cross-chain validity check causes permanent user-fund loss in Snowbridge relayer tip top-up flow - (File: `bridges/snowbridge/pallets/system-frontend/src/lib.rs`)

### Summary
`SnowbridgeSystemFrontend::add_tip` on AssetHub swaps a user-supplied asset for Ether and **burns it for teleportation unconditionally**, before it is known whether the target message nonce on BridgeHub is still pending. The teleported value is dispatched via `EthereumSystemV2::add_tip`, which, if the nonce has already been consumed (a normal race condition, not an attack), silently swallows the failure: it only increments a bookkeeping counter `LostTips` and still returns `Ok(())`. There is no extrinsic in the codebase that lets a user reclaim funds recorded in `LostTips`, so the burned/teleported value is permanently lost.

### Finding Description
The tip flow spans two chains:

1. On AssetHub, `add_tip` burns the tip asset unconditionally: [1](#0-0) 

2. `swap_fee_asset_and_burn` performs the swap and burn with no way to reverse it once done: [2](#0-1) 

3. The burned amount is forwarded via XCM `Transact` to BridgeHub's `EthereumSystemV2::add_tip`, which explicitly tolerates failure by recording it in `LostTips` and *still returns `Ok(())`* — masking the loss from the caller and providing no recovery path: [3](#0-2) 

4. `LostTips` is documented as merely aspirational bookkeeping — *"Capturing the lost tips here supports implementing a recovery method in the future"* — meaning no such recovery currently exists: [4](#0-3) 

5. The downstream `AddTip` implementations reject a tip as soon as the nonce is already consumed/processed, which is exactly the condition that triggers the loss path: [5](#0-4) [6](#0-5) 

The corrupted invariant: **settlement (burn/teleport of user funds on AH) advances before dispatch/execution success is confirmed on BH**, violating the required pivot that "message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically." Because relaying (nonce consumption) and tipping happen on different chains with unavoidable XCM message latency, there is an inherent, non-malicious race: a relayer can process/deliver the message between the moment a user submits `add_tip` on AH and the moment the XCM `Transact` executes on BH. This is not a "malicious relayer" scenario — it is a legitimate relayer doing its job at normal speed while a normal user is topping up a fee for a message that finishes just before the tip lands.

### Impact Explanation
This is a public, unprivileged entry point (`add_tip`, callable by any signed AssetHub account) where completely normal usage causes permanent, unrecoverable loss of user funds — precisely the "public underpriced work" / "permanent user-fund lock" impact class. The team's own `pr_9746` prdoc confirms this exact failure mode ("tips were not properly paid out, causing the tips to be lost since it had already been burnt") was previously hit in production code, underscoring that the design pattern (burn-before-confirm, "log-and-ignore" on failure) is a systemic weakness rather than a one-off bug, and `LostTips` still provides no way to reclaim value.

### Likelihood Explanation
High. No adversarial behavior, privileged access, or malicious actor is required — a normal relayer processing a message at typical speed while a user submits a tip for the same nonce is sufficient to trigger the loss. Given XCM cross-chain messaging latency (multiple blocks between AH `add_tip` submission and BH `EthereumSystemV2::add_tip` execution), this race condition is expected to occur under ordinary network activity, especially for nonces close to being relayed (which is exactly when users are most incentivized to add a tip to speed things up).

### Recommendation
- Do not burn/teleport tip funds on AssetHub until the target nonce's pendency is confirmed, or make the operation atomically reversible (e.g., hold funds in escrow on BH and only settle/refund based on confirmed nonce state).
- Have `EthereumSystemV2::add_tip` propagate failure (`Err`) instead of swallowing it via `LostTips` + `Ok(())`, or implement the promised "recovery method" now rather than deferring it, so `LostTips` balances are actually claimable by the original sender.
- Consider adding a nonce-liveness check on BH via a runtime API queryable from AH before committing to the burn, to reduce (not eliminate) the race window.

### Proof of Concept
1. User A submits `EthereumInboundQueueV2`/`EthereumOutboundQueueV2` message with nonce `N` and a relayer fee too low to be profitable.
2. Before it is relayed, User B calls `SnowbridgeSystemFrontend::add_tip(message_id = Inbound(N), asset)` on AssetHub to top up the fee. [1](#0-0) 
3. `swap_fee_asset_and_burn` swaps and burns User B's asset for Ether immediately, before any cross-chain confirmation.
4. In parallel, a relayer (behaving completely honestly, not maliciously) submits `submit_delivery_receipt`/`process_message` for nonce `N`, consuming it, before the tip's XCM `Transact` message reaches BridgeHub.
5. When the tip's `Transact` finally executes `EthereumSystemV2::add_tip` on BH, `InboundQueue::add_tip` returns `AddTipError::NonceConsumed` because `Nonce::<T>::get(nonce)` is already true: [5](#0-4) 
6. `EthereumSystemV2::add_tip` catches this `Err`, adds the amount to `LostTips::<T>::get(&sender)`, emits `TipProcessed { success: false }`, and still returns `Ok(())`: [7](#0-6) 
7. User B's Ether, already burned/teleported in step 3, is now permanently gone — recorded only as an unclaimable number in `LostTips`, with no extrinsic in the codebase to redeem it.

### Citations

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L261-273)
```rust
		pub fn add_tip(origin: OriginFor<T>, message_id: MessageId, asset: Asset) -> DispatchResult
		where
			<T as frame_system::Config>::AccountId: Into<Location>,
		{
			let who = ensure_signed(origin)?;

			let ether_gained = Self::swap_fee_asset_and_burn(who.clone().into(), asset)?;

			// Send the tip details to BH to be allocated to the reward in the Inbound/Outbound
			// pallet
			let call = Self::build_add_tip_call(who.clone(), message_id.clone(), ether_gained);
			Self::send_transact_call(who.into(), call)
		}
```

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L290-317)
```rust
		fn swap_and_burn(
			origin: Location,
			tip_asset_location: Location,
			ether_location: Location,
			tip_amount: u128,
		) -> Result<u128, DispatchError> {
			// Swap tip asset to ether
			let swap_path = vec![tip_asset_location.clone(), ether_location.clone()];
			let who = T::AccountIdConverter::convert_location(&origin)
				.ok_or(Error::<T>::LocationConversionFailed)?;

			let ether_gained = T::Swap::swap_exact_tokens_for_tokens(
				who.clone(),
				swap_path,
				tip_amount,
				None, // No minimum amount required
				who,
				true,
			)?;

			// Burn the ether
			let ether_asset = Asset::from((ether_location.clone(), ether_gained));

			burn_for_teleport::<T::AssetTransactor>(&origin, &ether_asset)
				.map_err(|_| Error::<T>::BurnError)?;

			Ok(ether_gained)
		}
```

**File:** bridges/snowbridge/pallets/system-v2/src/lib.rs (L136-142)
```rust
	/// Relayer reward tips that were paid by the user to incentivize the processing of their
	/// message, but then could not be added to their message reward (e.g. the nonce was already
	/// processed or their order could not be found). Capturing the lost tips here supports
	/// implementing a recovery method in the future.
	#[pallet::storage]
	pub type LostTips<T: Config> =
		StorageMap<_, Blake2_128Concat, AccountIdOf<T>, u128, ValueQuery>;
```

**File:** bridges/snowbridge/pallets/system-v2/src/lib.rs (L251-281)
```rust
		#[pallet::call_index(3)]
		#[pallet::weight(<T as pallet::Config>::WeightInfo::add_tip())]
		pub fn add_tip(
			origin: OriginFor<T>,
			sender: AccountIdOf<T>,
			message_id: MessageId,
			amount: u128,
		) -> DispatchResult {
			T::FrontendOrigin::ensure_origin(origin)?;

			let result = match message_id {
				Inbound(nonce) => <T as pallet::Config>::InboundQueue::add_tip(nonce, amount),
				Outbound(nonce) => <T as pallet::Config>::OutboundQueue::add_tip(nonce, amount),
			};

			if let Err(ref e) = result {
				tracing::debug!(target: LOG_TARGET, ?e, ?message_id, ?amount, "error adding tip");
				LostTips::<T>::mutate(&sender, |lost_tip| {
					*lost_tip = lost_tip.saturating_add(amount);
				});
			}

			Self::deposit_event(Event::<T>::TipProcessed {
				sender,
				message_id,
				amount,
				success: result.is_ok(),
			});

			Ok(())
		}
```

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs (L248-258)
```rust
	impl<T: Config> AddTip for Pallet<T> {
		fn add_tip(nonce: u64, amount: u128) -> Result<(), AddTipError> {
			ensure!(amount > 0, AddTipError::AmountZero);
			// If the nonce is already processed, return an error
			ensure!(!Nonce::<T>::get(nonce.into()), AddTipError::NonceConsumed);
			// Otherwise add the tip.
			Tips::<T>::mutate(nonce, |tip| {
				*tip = Some(tip.unwrap_or_default().saturating_add(amount));
			});
			return Ok(());
		}
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L483-495)
```rust
	impl<T: Config> AddTip for Pallet<T> {
		fn add_tip(nonce: u64, amount: u128) -> Result<(), AddTipError> {
			ensure!(amount > 0, AddTipError::AmountZero);
			PendingOrders::<T>::try_mutate_exists(nonce, |maybe_order| -> Result<(), AddTipError> {
				match maybe_order {
					Some(order) => {
						order.fee = order.fee.saturating_add(amount);
						Ok(())
					},
					None => Err(AddTipError::UnknownMessage),
				}
			})
		}
```
