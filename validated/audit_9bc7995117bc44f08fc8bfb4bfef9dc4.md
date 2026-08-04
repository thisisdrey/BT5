Confirmed: the flow is exactly analogous to the Caviar bug. On AssetHub, `snowbridge-pallet-system-frontend::add_tip` (`bridges/snowbridge/pallets/system-frontend/src/lib.rs:261-273`) irreversibly withdraws and burns the user's tip asset via `swap_fee_asset_and_burn`/`burn_for_teleport` *before* any confirmation that the tip will actually be applied, then dispatches a `Transact` to BridgeHub's `snowbridge-pallet-system-v2::add_tip`. On BridgeHub, that call always returns `Ok(())` even when the inner `AddTip::add_tip` fails, silently diverting the already-burned funds into an unclaimable `LostTips` counter.

### Title
Silent tip loss when BridgeHub `add_tip` fails after asset is already burned on AssetHub - (File: `bridges/snowbridge/pallets/system-v2/src/lib.rs`)

### Summary
`snowbridge-pallet-system-frontend::add_tip` burns/swaps the user's tip asset for Ether on AssetHub unconditionally, then forwards the tip amount via XCM `Transact` to `snowbridge-pallet-system-v2::add_tip` on BridgeHub. That extrinsic swallows any underlying failure from `InboundQueue::add_tip`/`OutboundQueue::add_tip` (e.g. `AddTipError::NonceConsumed` or `AddTipError::UnknownMessage`) and always returns `Ok(())`, merely bumping an unspendable `LostTips` counter instead of reverting or refunding. This mirrors the Caviar bug: the fee/royalty is collected from the payer unconditionally, but the actual "distribution" (crediting a relayer reward) is conditioned on a check that can fail, and the mismatch is never reconciled back to the payer.

### Finding Description
On AssetHub, `Pallet::add_tip` in `bridges/snowbridge/pallets/system-frontend/src/lib.rs:261-273` calls `swap_fee_asset_and_burn(who.clone().into(), asset)?`, which either swaps the tip asset for Ether via `T::Swap::swap_exact_tokens_for_tokens` or burns it directly via `burn_for_teleport::<T::AssetTransactor>` [1](#0-0) . This debits the user's real balance immediately and unconditionally, before the message even reaches BridgeHub [2](#0-1) .

The resulting `ether_gained` amount is packaged into an `EthereumSystemCall::AddTip { sender, message_id, amount }` and sent as an `UnpaidExecution`/`Transact` XCM to BridgeHub [3](#0-2) .

On BridgeHub, `snowbridge_pallet_system_v2::Pallet::add_tip` receives this call and forwards to the underlying queue's `AddTip::add_tip(nonce, amount)`: [4](#0-3) 

If the queue call fails — which happens whenever the nonce has already been processed (`AddTipError::NonceConsumed` in `bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs:249-258`) or the pending order no longer exists (`AddTipError::UnknownMessage` in `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs:483-495`) — the function does **not** propagate the error. It only records the amount into `LostTips::<T>` and still returns `Ok(())`: [5](#0-4) 

There is no code path anywhere in `snowbridge-pallet-system-v2`, `system-frontend`, or `snowbridge-core` (`bridges/snowbridge/primitives/core/src/reward.rs`) that reads `LostTips` and refunds or re-credits the user; the comment itself concedes this ("Capturing the lost tips here supports implementing a recovery method **in the future**") [6](#0-5) . Because the Ether was already burned via `burn_for_teleport` on AssetHub before the BridgeHub call, and because `Transact` execution success/failure inside the XCM is decoupled from whether the inner `add_tip` DispatchResult is `Ok`, the user's asset is permanently gone with nothing to show for it: no relayer reward registered, no tip applied, and no refund mechanism exists.

This is structurally identical to the Caviar bug: an amount is unconditionally taken from the payer up front based on an optimistic assumption (that the tip will apply), while the actual crediting operation is gated by a condition (`nonce` not yet consumed / order still pending) that the collection step does not verify, and the discrepancy is never reconciled.

### Impact Explanation
Because the nonce for a given outbound/inbound message can be consumed at any time by a relayer submitting a delivery receipt or the inbound message being processed, there is an unavoidable and easily triggerable race: a user calls `add_tip` on AssetHub (burning real value) targeting a `message_id` that gets finalized/removed from `PendingOrders`/its nonce marked processed on BridgeHub before (or concurrently with) the tip's `Transact` executing. The result is a guaranteed, permanent loss of the user's swapped/burned Ether-equivalent value with zero recovery path in the current pallet, which under the "permanent user-fund lock" impact category in the Polkadot SDK gate is a valid live-scope funds-loss finding.

### Likelihood Explanation
No privileged actor is required. Any signed AssetHub account can call `add_tip`; race conditions between tip submission and relayer delivery-receipt/message processing on BridgeHub are a normal, expected occurrence (the whole feature exists to let users add tips to speed up relaying of *pending* messages), so the failure branch (`NonceConsumed`/`UnknownMessage`) is reachable in ordinary, non-adversarial operation, not just through a malicious relayer.

### Recommendation
Either:
1. Make `system-v2::add_tip` return the queue's error to the caller (propagate `Err`) so the outer XCM `Transact` fails, and structure the AssetHub-side flow so the asset withdrawal/burn only happens after (or is reversible if) the BridgeHub-side registration fails (e.g. via a two-phase reserve/commit, or an XCM response acknowledging success before burning), or
2. Implement the promised recovery mechanism for `LostTips` now (a claim extrinsic that lets the recorded sender reclaim the lost amount in Ether/PNA form on BridgeHub or via XCM back to AssetHub), rather than leaving it as a permanently stranded counter.

### Proof of Concept
1. Relayer submits `submit_delivery_receipt` for outbound nonce `N`, which removes `PendingOrders[N]` in `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs:475` (or an inbound message for nonce `N` is processed, setting `Nonce::<T>::set(nonce)` in `bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs:225`).
2. Concurrently/shortly after, a user on AssetHub calls `EthereumSystemFrontend::add_tip(origin, MessageId::Outbound(N), asset)` intending to boost the relayer reward for message `N`.
3. `swap_fee_asset_and_burn` executes and burns/swaps the user's asset for Ether immediately (`bridges/snowbridge/pallets/system-frontend/src/lib.rs:372-404`), debiting the user.
4. The `Transact` reaches BridgeHub; `snowbridge_pallet_system_v2::add_tip` calls `OutboundQueue::add_tip(N, amount)`, which returns `Err(AddTipError::UnknownMessage)` because the order was already removed (`bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs:483-495`).
5. `system-v2::add_tip` swallows this error, adds `amount` to `LostTips::<T>::get(sender)`, emits `TipProcessed { success: false }`, and returns `Ok(())` (`bridges/snowbridge/pallets/system-v2/src/lib.rs:251-281`).
6. The user's burned Ether is gone; the relayer never receives a boosted reward; `LostTips` has no consumer anywhere in the codebase, so the funds are permanently unrecoverable.

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

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L340-363)
```rust
		// Build the call to dispatch the `EthereumSystem::add_tip` extrinsic on BH
		fn build_add_tip_call(
			sender: AccountIdOf<T>,
			message_id: MessageId,
			amount: u128,
		) -> BridgeHubRuntime<T> {
			BridgeHubRuntime::EthereumSystem(EthereumSystemCall::AddTip {
				sender,
				message_id,
				amount,
			})
		}

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
