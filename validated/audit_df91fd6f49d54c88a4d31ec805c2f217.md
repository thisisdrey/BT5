### Title
Irreversible fund burn in `add_tip` before validating that the target message is still tippable - ([File: bridges/snowbridge/pallets/system-frontend/src/lib.rs])

### Summary
The Snowbridge tipping flow burns a user's asset on the source chain (AssetHub) *before* verifying, on the destination chain (BridgeHub), whether the tipped message is still in a state that can accept a tip. If the underlying message has already been processed (nonce consumed) by the time the asynchronous XCM `Transact` arrives, BridgeHub rejects the tip registration, but the user's asset was already irreversibly burned on AssetHub with no refund path — mirroring the reported class of bug where a stale/expired state is not checked before committing an irreversible action.

### Finding Description
`Pallet::add_tip` in `bridges/snowbridge/pallets/system-frontend/src/lib.rs` is a public, permissionless extrinsic callable by any signed account: [1](#0-0) 

It immediately swaps the caller's tip asset and **burns** it for teleportation via `swap_fee_asset_and_burn` → `burn_for_teleport`: [2](#0-1) 

Only *after* the burn does it construct an `EthereumSystemCall::AddTip { sender, message_id, amount }` and dispatch it as a **one-way, unpaid** XCM `Transact` to BridgeHub:

<cite repo="Lauraivanka/polkadot-sdk--014" path="bridges/snowbridge/pallets/system-frontend/src/lib.rs" start="340="/> [3](#0-2) 

`Xcm::UnpaidExecution` + `Transact` is fire-and-forget: there is no acknowledgment, no callback, and no rollback of the burn on AssetHub if the `Transact` fails on BridgeHub.

On the BridgeHub side, the actual validation of whether a tip can still be attached happens only when the `Transact` executes, in `AddTip::add_tip` of `snowbridge-pallet-inbound-queue-v2`: [4](#0-3) 

This checks `!Nonce::<T>::get(nonce)` — i.e., whether the message has already been relayed/processed. Message processing (`process_message`) marks the nonce as consumed *before* it even attempts dispatch, and independently drains any previously-registered tip: [5](#0-4) 

Because relaying of the underlying Ethereum message (`submit`/`process_message` on BridgeHub) and the user's tipping flow (`add_tip` on AssetHub → XCM `Transact` to BridgeHub) are two independent, asynchronous paths with no shared lock, there is a race window: a relayer can call `submit` and consume the nonce for a given message at any time. If a user's `add_tip` call races with — or simply arrives after — the message has already been relayed (a common, natural occurrence since relaying is driven independently by off-chain relayers, not by the tipper), then:
1. The user's fee asset has already been swapped and burned on AssetHub (irreversible).
2. The `AddTip` `Transact` on BridgeHub fails with `AddTipError::NonceConsumed`.
3. There is no compensating transaction, refund, or trapped-asset recovery path defined in this flow — the burned value is permanently lost to the user.

This is the direct analog of the reported `handleSenderPrepare` issue: the source-chain action ("prepare"/burn) is committed based on stale assumptions about "fulfillability" (whether the tip target is still valid) without re-validating expiry/consumption state, and there is no lock coordinating the two chains' state.

### Impact Explanation
Any unprivileged user calling `add_tip` can permanently lose the burned tip asset with no path to recovery, whenever the tipped message has already been (or gets, during the async delay) processed on BridgeHub. This is a "permanent user-fund lock/loss" impact under the accepted impact categories, triggered purely by public entrypoints and normal asynchronous cross-chain timing — no malicious relayer, validator, or governance actor is required.

### Likelihood Explanation
The race is not a contrived edge case: relaying of Ethereum messages into BridgeHub via `submit` happens continuously and independently of any specific user's tipping decision. A user submitting a tip for a message that is already in-flight to being relayed (or is relayed in the same or an adjacent block as the `add_tip` XCM `Transact` arrives) will reliably hit this condition. Given the multi-block latency of cross-chain XCM `Transact` delivery between AssetHub and BridgeHub, the window in which a message can be relayed before the tip lands is realistically wide, making this a likely occurrence rather than a rare edge case.

### Recommendation
- Do not burn the tip asset on AssetHub until confirmation that the message/nonce is still unprocessed on BridgeHub, or
- Use a paid/acknowledged XCM pattern (e.g., query-response or a receipt-based flow) so that a failure of `AddTip::add_tip` on BridgeHub can trigger a refund or mint-back of the burned asset on AssetHub, or
- Add a matching, up-front check on AssetHub (via a runtime API / cached nonce state) before burning, and treat this as best-effort protection combined with a settlement/trap-and-claim mechanism for the failure case, similar to how `Message.claimer` already exists for other trapped-fund scenarios in this bridge.

### Proof of Concept
1. A message with nonce `N` is bridged from Ethereum and is pending relay (not yet consumed) on BridgeHub.
2. User calls `add_tip(message_id = N, asset)` on AssetHub. `swap_fee_asset_and_burn` swaps and burns the user's asset (`burn_for_teleport`), then sends `Transact(EthereumSystemCall::AddTip { sender, message_id: N, amount })` to BridgeHub as `UnpaidExecution`.
3. Before this XCM `Transact` is executed on BridgeHub, a relayer calls `submit` for nonce `N`; `process_message` sets `Nonce::<T>::set(N)` and processes the message, taking whatever tip (none) was registered at that time via `Tips::<T>::take(N)`.
4. The delayed `Transact` for `AddTip` finally executes on BridgeHub; `AddTip::add_tip` sees `Nonce::<T>::get(N) == true` and returns `AddTipError::NonceConsumed`, so the tip is never registered and no reward is paid.
5. The user's originally burned asset on AssetHub is gone — there is no compensating credit, refund, or reversal anywhere in `system-frontend`, `system-v2`, or `inbound-queue-v2`.

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

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L353-363)
```rust
		fn build_remote_xcm(call: &impl Encode) -> Xcm<()> {
			Xcm(vec![
				DescendOrigin(T::PalletLocation::get()),
				UnpaidExecution { weight_limit: Unlimited, check_origin: None },
				Transact {
					origin_kind: OriginKind::Xcm,
					call: call.encode().into(),
					fallback_max_weight: None,
				},
			])
		}
```

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L372-404)
```rust
		fn swap_fee_asset_and_burn(
			origin: Location,
			fee_asset: Asset,
		) -> Result<u128, DispatchError> {
			let ether_location = T::EthereumLocation::get();
			let (fee_asset_location, fee_amount) = match fee_asset {
				Asset { id: AssetId(ref loc), fun: Fungible(amount) } => (loc, amount),
				_ => {
					tracing::debug!(target: LOG_TARGET, ?fee_asset, "error matching fee asset");
					return Err(Error::<T>::UnsupportedAsset.into());
				},
			};
			if fee_amount == 0 {
				return Ok(0);
			}

			let ether_gained = if *fee_asset_location != ether_location {
				Self::swap_and_burn(
					origin.clone(),
					fee_asset_location.clone(),
					ether_location,
					fee_amount,
				)
				.inspect_err(|&e| {
					tracing::debug!(target: LOG_TARGET, ?e, "error swapping asset");
				})?
			} else {
				burn_for_teleport::<T::AssetTransactor>(&origin, &fee_asset)
					.map_err(|_| Error::<T>::BurnError)?;
				fee_amount
			};
			Ok(ether_gained)
		}
```

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs (L214-245)
```rust
	impl<T: Config> Pallet<T> {
		pub fn process_message(relayer: T::AccountId, message: Message) -> DispatchResult {
			// Verify that the message was submitted from the known Gateway contract
			ensure!(T::GatewayAddress::get() == message.gateway, Error::<T>::InvalidGateway);

			let (nonce, relayer_fee) = (message.nonce, message.relayer_fee);

			// Verify the message has not been processed
			ensure!(!Nonce::<T>::get(nonce), Error::<T>::InvalidNonce);

			// Mark message as received
			Nonce::<T>::set(nonce);

			let message_id = T::MessageProcessor::process_message(relayer.clone(), message)
				.map_err(|e| match e {
					MessageProcessorError::ProcessMessage(e) => e,
					MessageProcessorError::ConvertMessage(e) => Error::<T>::from(e).into(),
					MessageProcessorError::SendMessage(e) => Error::<T>::from(e).into(),
				})?;

			// Pay relayer reward
			let tip = Tips::<T>::take(nonce).unwrap_or_default();
			let total_tip = relayer_fee.saturating_add(tip);
			if total_tip > 0 {
				T::RewardPayment::register_reward(&relayer, T::DefaultRewardKind::get(), total_tip);
			}

			// Emit event with the message_id
			Self::deposit_event(Event::MessageReceived { nonce, message_id });

			Ok(())
		}
```

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs (L248-259)
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
	}
```
