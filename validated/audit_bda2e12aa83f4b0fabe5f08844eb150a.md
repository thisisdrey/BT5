I've confirmed the code matches the claim precisely. The analysis is well-supported: `register_token` checks `Self::export_operating_mode().is_halted()` before proceeding, while `add_tip` in the system-frontend pallet has no such check and proceeds directly to swap/burn Ether and dispatch a `Transact` to BridgeHub. The BridgeHub-side `add_tip` in `system-v2` also lacks any operating-mode/halt check, simply forwarding to `InboundQueue::add_tip`/`OutboundQueue::add_tip` which only validate nonce/amount, not halt state.

Audit Report

## Title
`add_tip` in `SnowbridgeSystemFrontend` bypasses the pause/halt guard, letting users burn real value while message export is supposed to be halted - (File: `bridges/snowbridge/pallets/system-frontend/src/lib.rs`)

## Summary
The Snowbridge system-frontend pallet's `ExportOperatingMode` halt flag is checked by `register_token` but not by `add_tip`, which is a public, signed-origin dispatchable that swaps a user-supplied asset for Ether, burns it via `burn_for_teleport`, and dispatches an XCM `Transact` to BridgeHub. This allows an unprivileged user to burn real asset value and inject cross-chain traffic even while the bridge has been explicitly halted via `set_operating_mode`.

## Finding Description
`ExportOperatingMode<T>` is the pallet's authoritative halt flag, and `ExportPausedQuery::is_paused()` reads it directly. [1](#0-0)  `register_token` correctly enforces this via `ensure!(!Self::export_operating_mode().is_halted(), Error::<T>::Halted);` before performing any state changes. [2](#0-1)  In contrast, `add_tip` goes straight from `ensure_signed` into `Self::swap_fee_asset_and_burn` (which swaps the tip asset for Ether via `T::Swap::swap_exact_tokens_for_tokens` and burns it via `burn_for_teleport`) and then calls `Self::send_transact_call` to dispatch an XCM `Transact` toward BridgeHub — with no halt check anywhere in the path. [3](#0-2)  The underlying burn/swap logic in `swap_fee_asset_and_burn` and `swap_and_burn` also contains no operating-mode check. [4](#0-3)  On the BridgeHub side, `snowbridge_pallet_system_v2::add_tip` likewise performs no halt/operating-mode check, forwarding directly to `InboundQueue::add_tip`/`OutboundQueue::add_tip` (which only validate `amount > 0` and nonce-not-consumed) and recording `LostTips` on failure. [5](#0-4)  The `AddTip` implementations in `inbound-queue-v2` and `outbound-queue-v2` confirm no operating-mode gating exists at that layer either. [6](#0-5) [7](#0-6) 

## Impact Explanation
When the bridge is halted via `set_operating_mode(Halted)`, an unprivileged signed user can still call `add_tip`, causing real backing-asset value (swapped into Ether and irreversibly burned via `burn_for_teleport`) to be destroyed and a cross-chain `Transact` to be dispatched to BridgeHub, even though the pause is meant to fully stop Ethereum-bound export activity. This matches the "public underpriced work that degrades... stalls bridge processing" and asset-burn-while-halted impact class, since value is destroyed on the Polkadot side while the corresponding Ethereum-side processing is supposed to be frozen.

## Likelihood Explanation
High: `add_tip` is a normal signed extrinsic requiring no special privilege; the missing check is a simple code-path omission that is present in the sibling `register_token` function but absent here. Any user can invoke it at any time while the pallet is halted, with no race condition or additional external actor needed.

## Recommendation
Add the same halt check used in `register_token` to `add_tip`:
```rust
pub fn add_tip(origin: OriginFor<T>, message_id: MessageId, asset: Asset) -> DispatchResult {
    ensure!(!Self::export_operating_mode().is_halted(), Error::<T>::Halted);
    let who = ensure_signed(origin)?;
    ...
}
```
Additionally audit `snowbridge_pallet_system_v2::add_tip` and the `AddTip` implementations in `inbound-queue-v2`/`outbound-queue-v2` to ensure operating-mode/halt state is consistently consulted across all value-moving and message-forwarding entry points.

## Proof of Concept
1. Root calls `SnowbridgeSystemFrontend::set_operating_mode(Halted)`, setting `ExportOperatingMode::Halted`.
2. A signed user calls `SnowbridgeSystemFrontend::add_tip(origin, message_id, fee_asset)`.
3. Execution proceeds through `swap_fee_asset_and_burn` (swap + burn) and `send_transact_call` (XCM `Transact` to BridgeHub) without any `Error::Halted` rejection, unlike calling `register_token` in the same state, which correctly fails.
4. A unit test in `bridges/snowbridge/pallets/system-frontend/src/tests.rs` mirroring `register_token`'s halted-mode test but for `add_tip` would confirm the extrinsic succeeds despite `ExportOperatingMode::Halted`.

### Citations

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L225-235)
```rust
		pub fn register_token(
			origin: OriginFor<T>,
			asset_id: Box<VersionedLocation>,
			metadata: AssetMetadata,
			fee_asset: Asset,
		) -> DispatchResult {
			ensure!(!Self::export_operating_mode().is_halted(), Error::<T>::Halted);

			let asset_location: Location =
				(*asset_id).try_into().map_err(|_| Error::<T>::UnsupportedLocationVersion)?;
			let origin_location = T::RegisterTokenOrigin::ensure_origin(origin, &asset_location)?;
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

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L426-430)
```rust
	impl<T: Config> ExportPausedQuery for Pallet<T> {
		fn is_paused() -> bool {
			Self::export_operating_mode().is_halted()
		}
	}
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
