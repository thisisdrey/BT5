## Analysis

Confirmed: `LostTips<T>` in `snowbridge-pallet-system-v2` is only ever written to (`bridges/snowbridge/pallets/system-v2/src/lib.rs:141-142, 268-270`), and there is no dispatchable, hook, or trait method anywhere in the repository that reads and pays out or clears `LostTips`. The comment on the storage item explicitly acknowledges this: *"Capturing the lost tips here supports implementing a recovery method in the future"* [1](#0-0) , i.e. the recovery/refund path is not implemented, exactly mirroring the `BaseAsyncSwap` bug class (value held by a contract/pallet with no way to distribute, withdraw, or refund it back to the depositor).

### Title
Snowbridge tip funds are irrecoverably burned and lost on remote-dispatch failure with no refund/claim mechanism - (File: `bridges/snowbridge/pallets/system-v2/src/lib.rs`, `bridges/snowbridge/pallets/system-frontend/src/lib.rs`)

### Summary
`pallet-snowbridge-system-frontend::add_tip` on AssetHub swaps the user's tip asset for Ether and irreversibly burns it via `burn_for_teleport` *before* dispatching an XCM `Transact` to BridgeHub's `snowbridge-pallet-system-v2::add_tip`. If that remote call fails (e.g. the target nonce was already consumed, or the `PendingOrder`/outbound entry no longer exists), the already-burned value is merely recorded into a `LostTips` storage map keyed by sender, with the pallet's own documentation admitting no recovery mechanism exists yet.

### Finding Description
On AssetHub, `add_tip` withdraws/burns the user's real value up front: [2](#0-1) 

`swap_fee_asset_and_burn`/`swap_and_burn` perform the swap and call `burn_for_teleport::<T::AssetTransactor>`, which destroys the asset on AssetHub (this happens synchronously, in the same extrinsic, before any XCM has been delivered or executed on BridgeHub): [3](#0-2) 

The burned value is only "recreated" as an ether tip reward if the remote `Transact` call on BridgeHub succeeds. On BridgeHub, `EthereumSystemV2::add_tip` forwards to the inbound/outbound queue's `AddTip::add_tip`: [4](#0-3) 

If the underlying queue rejects the tip — `AddTipError::NonceConsumed` in the inbound queue (`bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs:248-259`) or `AddTipError::UnknownMessage` in the outbound queue (`bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs:483-496`) — the failure is swallowed and only bookkept: [5](#0-4) 

`LostTips` is declared with a comment admitting the recovery path doesn't exist: [1](#0-0) 

A grep across the repository confirms `LostTips` is never read anywhere except in tests that assert it was incremented — there is no extrinsic, hook, or `OnIdle`/governance call that pays it back out to the `sender`. This is the direct analog of the `BaseAsyncSwap` finding: value is taken from a user and held/tracked in a bespoke accounting structure, but the pallet lacks any documented or implemented "distribute, use, withdraw, or refund" utility for the recorded balance, and the failure path is entirely reachable by an ordinary unprivileged signed user simply calling `add_tip` for a message whose nonce is processed (or races) before the remote `Transact` executes.

### Impact Explanation
This is a genuine, permanent user-fund loss on Snowbridge's BridgeHub/AssetHub delivery flow: real DOT/other assets are irreversibly burned on AssetHub, and if the remote tip-registration call fails for any reason (race between message processing and tip submission, outbound order already committed/removed, etc.), the corresponding ether value is never minted into any reward and is not refundable to the user — it is stuck forever as a dangling counter in `LostTips`. This falls squarely within the "permanent user-fund or bridge-state lock" and "duplicate settlement or payout" (here: non-payout) categories of the accepted impact gate, and requires no malicious peer, relayer, validator, or governance actor — only an ordinary signed extrinsic timed against normal, expected relayer/message processing.

### Likelihood Explanation
High. The race is trivial and can occur under entirely benign, expected operation: a relayer can process/consume the target message's nonce (inbound queue) or the outbound order can be delivered/removed before the user's `add_tip` XCM Transact executes on BridgeHub — there is no atomicity or ordering guarantee tying the AssetHub burn to the BridgeHub-side tip registration success. No adversarial actor is required; ordinary network latency between the two chains' block production is sufficient.

### Recommendation
Do not burn/withdraw the tip asset on AssetHub until success of the remote registration is confirmed, or implement a documented recovery/refund extrinsic that lets the original `sender` (verified via `LostTips`) reclaim/re-mint the corresponding value (e.g., a `claim_lost_tip` call gated on `LostTips::<T>::take(sender)` that mints/teleports the amount back to them on BridgeHub or notifies AssetHub to do so). At minimum, add a receipt/refund callback so failed remote registration reverses the burn atomically instead of leaving it in a write-only ledger.

### Proof of Concept
1. User calls `SnowbridgeSystemFrontend::add_tip(origin, MessageId::Inbound(N), asset)` on AssetHub for inbound nonce `N` that has not yet been processed.
2. `swap_fee_asset_and_burn` swaps the asset for ether and calls `burn_for_teleport`, irreversibly destroying `amount` on AssetHub (`bridges/snowbridge/pallets/system-frontend/src/lib.rs:267,312-314`).
3. The XCM `Transact` carrying `EthereumSystemCall::AddTip{ sender, message_id: Inbound(N), amount }` is sent to BridgeHub.
4. Before or concurrently with the XCM's execution, a relayer submits and processes message `N` via `InboundQueue::submit`, setting `Nonce::<T>::set(N)`.
5. When the delayed `Transact` executes, `EthereumSystemV2::add_tip` calls `InboundQueue::add_tip(N, amount)`, which now returns `Err(AddTipError::NonceConsumed)` (`bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs:252`).
6. `EthereumSystemV2::add_tip` catches the error and only does `LostTips::<T>::mutate(&sender, |lost_tip| *lost_tip = lost_tip.saturating_add(amount))` (`bridges/snowbridge/pallets/system-v2/src/lib.rs:266-270`), emitting `TipProcessed{ success: false }`.
7. The user's `amount` was already burned in step 2 and is now permanently unrecoverable — no extrinsic exists to reclaim the value tracked in `LostTips`.

### Citations

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

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L286-317)
```rust

		/// Swaps a specified tip asset to Ether and then burns the resulting ether for
		/// teleportation. Returns the amount of Ether gained if successful, or a DispatchError if
		/// any step fails.
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
