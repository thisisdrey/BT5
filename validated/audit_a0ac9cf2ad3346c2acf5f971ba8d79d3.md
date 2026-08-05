The claim is fully verified against the actual source code. `register_token` at line 231 contains the halted-mode check `ensure!(!Self::export_operating_mode().is_halted(), Error::<T>::Halted);` before calling `Self::swap_fee_asset_and_burn`, while `add_tip` at lines 261-273 has no such check and goes directly from `ensure_signed(origin)?` to `Self::swap_fee_asset_and_burn(who.clone().into(), asset)?`. Both extrinsics share the identical fund-burning helper `swap_fee_asset_and_burn` which calls `burn_for_teleport::<T::AssetTransactor>` (an irreversible burn). The `ExportPausedQuery` implementation at lines 426-430 confirms `ExportOperatingMode` is meant to be an authoritative halt signal for the export path.

Audit Report

## Title
`add_tip` missing halted-mode check allows fund burn while Snowbridge export is paused - (File: `bridges/snowbridge/pallets/system-frontend/src/lib.rs`)

## Summary
The `snowbridge-pallet-system-frontend` pallet's `add_tip` extrinsic omits the `ExportOperatingMode` halted check that its sibling extrinsic `register_token` correctly enforces, despite both calling the same fund-burning helper `swap_fee_asset_and_burn`. This lets any signed user irreversibly burn tip assets and dispatch a `Transact` XCM to BridgeHub even while the pallet owner has explicitly halted export operations.

## Finding Description
`register_token` guards its call to `Self::swap_fee_asset_and_burn(...)` with `ensure!(!Self::export_operating_mode().is_halted(), Error::<T>::Halted);` at [1](#0-0) . In contrast, `add_tip` proceeds directly from `ensure_signed(origin)?` to `Self::swap_fee_asset_and_burn(who.clone().into(), asset)?` with no halted check at all: [2](#0-1) .

Both extrinsics funnel into the same helper `swap_fee_asset_and_burn`, which swaps the supplied asset for Ether (or burns it directly if already Ether) via `burn_for_teleport::<T::AssetTransactor>`, an irreversible action: [3](#0-2) .

The `ExportOperatingMode` storage is exposed via `ExportPausedQuery::is_paused` as the authoritative pause signal for the export path: [4](#0-3) . Because `add_tip` bypasses the halted check present in `register_token`, the pause mechanism does not stop the burn side effect for tips, which is an asymmetry between two public, unprivileged extrinsics sharing identical fund-destructive logic.

## Impact Explanation
This matches the "permanent user-fund lock" / degraded bridge processing impact class. When the pallet is halted (e.g., during an incident on the Ethereum side or a compromised relay), `add_tip` still allows callers to irrecoverably burn their fee/tip assets and dispatch a `Transact` call to the backend `EthereumSystem::add_tip` on BridgeHub, defeating the intended emergency freeze and potentially producing burned-but-unprocessed value if the backend is also paused or misbehaving during the incident.

## Likelihood Explanation
High likelihood and directly reproducible: no privileged access is required. Any signed account can call `add_tip` with an arbitrary `asset` at any time, including during a halt, since the code path contains no gating logic whatsoever.

## Recommendation
Add the same halted-mode guard used in `register_token` to `add_tip`, immediately after `ensure_signed(origin)?` and before calling `swap_fee_asset_and_burn`. Consider moving the check inside `swap_fee_asset_and_burn` itself so future callers of this shared fund-burning helper cannot omit it.

## Proof of Concept
1. Root calls `set_operating_mode(Halted)`.
2. Confirm `register_token` is rejected with `Error::<T>::Halted` (per existing test `test_switch_operating_mode`).
3. Call `EthereumSystemFrontend::add_tip(RuntimeOrigin::signed(who), message_id, asset)` with a valid asset.
4. Observe the call succeeds, emits `Event::MessageSent`, and burns the tip asset despite the pallet being in `Halted` mode — confirming the missing guard in `add_tip` at [2](#0-1) .

### Citations

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L230-242)
```rust
		) -> DispatchResult {
			ensure!(!Self::export_operating_mode().is_halted(), Error::<T>::Halted);

			let asset_location: Location =
				(*asset_id).try_into().map_err(|_| Error::<T>::UnsupportedLocationVersion)?;
			let origin_location = T::RegisterTokenOrigin::ensure_origin(origin, &asset_location)?;

			let ether_gained = if origin_location.is_here() {
				// Root origin/location does not pay any fees/tip.
				0
			} else {
				Self::swap_fee_asset_and_burn(origin_location.clone(), fee_asset)?
			};
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
