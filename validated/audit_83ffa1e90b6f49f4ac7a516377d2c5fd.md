All code citations in the claim check out exactly against the repository. `snowbridge-pallet-system-frontend::add_tip` at `bridges/snowbridge/pallets/system-frontend/src/lib.rs:261-273` unconditionally calls `swap_fee_asset_and_burn` (lines 372-404), which either swaps via `T::Swap::swap_exact_tokens_for_tokens` or burns directly via `burn_for_teleport` — irreversibly debiting the user — before dispatching an `UnpaidExecution`/`Transact` XCM to BridgeHub built in `build_remote_xcm` (lines 353-363). On BridgeHub, `snowbridge_pallet_system_v2::Pallet::add_tip` (lines 251-281) forwards to `InboundQueue::add_tip`/`OutboundQueue::add_tip`, and confirmed the exact failure conditions: `AddTipError::NonceConsumed` is returned when `Nonce::<T>::get(nonce)` is already true in `inbound-queue-v2/src/lib.rs:248-259`, and `AddTipError::UnknownMessage` is returned when `PendingOrders::<T>` no longer contains the nonce in `outbound-queue-v2/src/lib.rs:483-496`. In both cases `system-v2::add_tip` swallows the error into `LostTips::<T>::mutate` and still returns `Ok(())`.

I also confirmed there is no consumer of `LostTips` anywhere in the codebase outside of tests — it is only written to, never read/refunded, matching the claim that no recovery path exists. `AddTipError` and `AddTip` trait are defined in `bridges/snowbridge/primitives/core/src/reward.rs:32-43` with no linkage back to a refund mechanism.

All claimed code paths, line ranges, and logical flow are accurate and verified against the actual source.

Audit Report

## Title
Silent tip loss when BridgeHub `add_tip` fails after asset is already burned on AssetHub - (File: `bridges/snowbridge/pallets/system-v2/src/lib.rs`)

## Summary
`snowbridge-pallet-system-frontend::add_tip` burns/swaps the user's tip asset for Ether on AssetHub unconditionally, then forwards the tip amount via XCM `Transact` to `snowbridge-pallet-system-v2::add_tip` on BridgeHub. That extrinsic swallows any underlying failure from `InboundQueue::add_tip`/`OutboundQueue::add_tip` (e.g. `AddTipError::NonceConsumed` or `AddTipError::UnknownMessage`) and always returns `Ok(())`, merely bumping an unspendable `LostTips` counter instead of reverting or refunding.

## Finding Description
On AssetHub, `Pallet::add_tip` in `bridges/snowbridge/pallets/system-frontend/src/lib.rs:261-273` calls `swap_fee_asset_and_burn(who.clone().into(), asset)?`, which either swaps the tip asset for Ether via `T::Swap::swap_exact_tokens_for_tokens` or burns it directly via `burn_for_teleport::<T::AssetTransactor>` (`system-frontend/src/lib.rs:372-404`). This debits the user's real balance immediately and unconditionally, before the message even reaches BridgeHub. The resulting `ether_gained` amount is packaged into an `EthereumSystemCall::AddTip { sender, message_id, amount }` and sent as an `UnpaidExecution`/`Transact` XCM to BridgeHub (`system-frontend/src/lib.rs:340-363`).

On BridgeHub, `snowbridge_pallet_system_v2::Pallet::add_tip` (`system-v2/src/lib.rs:251-281`) forwards to the underlying queue's `AddTip::add_tip(nonce, amount)`. If the queue call fails — which happens whenever the nonce has already been processed (`AddTipError::NonceConsumed`, `inbound-queue-v2/src/lib.rs:248-259`) or the pending order no longer exists (`AddTipError::UnknownMessage`, `outbound-queue-v2/src/lib.rs:483-496`) — the function does **not** propagate the error. It only records the amount into `LostTips::<T>` (`system-v2/src/lib.rs:266-271`) and still returns `Ok(())`.

There is no code path anywhere in `snowbridge-pallet-system-v2`, `system-frontend`, or `snowbridge-core::reward` that reads `LostTips` and refunds or re-credits the user; the storage doc comment itself concedes this ("Capturing the lost tips here supports implementing a recovery method **in the future**", `system-v2/src/lib.rs:136-139`). Because the Ether was already burned via `burn_for_teleport` on AssetHub before the BridgeHub call, and because `Transact` execution success/failure inside the XCM is decoupled from whether the inner `add_tip` `DispatchResult` is `Ok`, the user's asset is permanently gone with nothing to show for it: no relayer reward registered, no tip applied, and no refund mechanism exists.

## Impact Explanation
Because the nonce for a given outbound/inbound message can be consumed at any time by a relayer submitting a delivery receipt (removing `PendingOrders[nonce]` at `outbound-queue-v2/src/lib.rs:475`) or by inbound message processing (`Nonce::<T>::set(nonce)` at `inbound-queue-v2/src/lib.rs:225`), there is an unavoidable and easily triggerable race: a user calls `add_tip` on AssetHub (burning real value) targeting a `message_id` that gets finalized/removed on BridgeHub concurrently with the tip's `Transact` executing. The result is a guaranteed, permanent loss of the user's swapped/burned Ether-equivalent value with zero recovery path in the current pallet, matching the "permanent user-fund lock" impact category in the Polkadot SDK impact gate.

## Likelihood Explanation
No privileged actor is required. Any signed AssetHub account can call `add_tip`; race conditions between tip submission and relayer delivery-receipt/message processing on BridgeHub are a normal, expected occurrence (the whole feature exists to let users add tips to speed up relaying of *pending* messages), so the failure branch (`NonceConsumed`/`UnknownMessage`) is reachable in ordinary, non-adversarial operation, not just through a malicious relayer.

## Recommendation
Either:
1. Make `system-v2::add_tip` return the queue's error to the caller (propagate `Err`) so the outer XCM `Transact` fails, and structure the AssetHub-side flow so the asset withdrawal/burn only happens after (or is reversible if) the BridgeHub-side registration fails (e.g. via a two-phase reserve/commit, or an XCM response acknowledging success before burning), or
2. Implement the promised recovery mechanism for `LostTips` now (a claim extrinsic that lets the recorded sender reclaim the lost amount in Ether/PNA form on BridgeHub or via XCM back to AssetHub), rather than leaving it as a permanently stranded counter.

## Proof of Concept
1. Relayer submits `submit_delivery_receipt` for outbound nonce `N`, which removes `PendingOrders[N]` in `outbound-queue-v2/src/lib.rs:475` (or an inbound message for nonce `N` is processed, setting `Nonce::<T>::set(nonce)` in `inbound-queue-v2/src/lib.rs:225`).
2. Concurrently/shortly after, a user on AssetHub calls `EthereumSystemFrontend::add_tip(origin, MessageId::Outbound(N), asset)` intending to boost the relayer reward for message `N`.
3. `swap_fee_asset_and_burn` executes and burns/swaps the user's asset for Ether immediately (`system-frontend/src/lib.rs:372-404`), debiting the user.
4. The `Transact` reaches BridgeHub; `snowbridge_pallet_system_v2::add_tip` calls `OutboundQueue::add_tip(N, amount)`, which returns `Err(AddTipError::UnknownMessage)` because the order was already removed (`outbound-queue-v2/src/lib.rs:483-496`).
5. `system-v2::add_tip` swallows this error, adds `amount` to `LostTips::<T>::get(sender)`, emits `TipProcessed { success: false }`, and returns `Ok(())` (`system-v2/src/lib.rs:251-281`).
6. The user's burned Ether is gone; the relayer never receives a boosted reward; `LostTips` has no consumer anywhere in the codebase, so the funds are permanently unrecoverable. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6) [8](#0-7)

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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L483-496)
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

**File:** bridges/snowbridge/primitives/core/src/reward.rs (L32-43)
```rust
#[derive(Debug, Encode, PartialEq, DecodeWithMemTracking, Decode, TypeInfo, PalletError)]
pub enum AddTipError {
	NonceConsumed,
	UnknownMessage,
	AmountZero,
}

/// Trait to add a tip for a nonce.
pub trait AddTip {
	/// Add a relayer reward tip to a pallet.
	fn add_tip(nonce: u64, amount: u128) -> Result<(), AddTipError>;
}
```
