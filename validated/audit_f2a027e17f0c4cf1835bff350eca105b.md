This confirms the claim precisely. The `Swap` trait's `swap_exact_tokens_for_tokens` explicitly supports an `amount_out_min: Option<Self::Balance>` parameter that, when `Some`, causes the swap to error out if the minimum isn't met, as documented at [1](#0-0) . The `system-frontend` pallet's `swap_and_burn` deliberately hardcodes this to `None`, discarding any slippage protection entirely, at [2](#0-1) . Both `register_token` (unprivileged/publicly reachable for non-`Here` origins) and `add_tip` (unprivileged, `ensure_signed`) route through `swap_fee_asset_and_burn` → `swap_and_burn` with no slippage bound, per [3](#0-2)  and [4](#0-3) . The resulting `ether_gained` is burned via `burn_for_teleport` and forwarded verbatim as the `amount` in `EthereumSystemCall::AddTip`/`RegisterToken`, which is trusted as-is downstream in `inbound-queue-v2`'s `AddTip::add_tip` (`Tips` storage) as shown in the claim's citations.

This matches the actual codebase state and represents a genuine unbounded-slippage swap in a publicly reachable extrinsic, satisfying the "public underpriced work" / value-loss impact criteria.

Audit Report

## Title
Unbounded-slippage swap lets `add_tip`/`register_token` record and burn a mismatched Ether amount - (File: `bridges/snowbridge/pallets/system-frontend/src/lib.rs`)

## Summary
`Pallet::<T>::add_tip` and `Pallet::<T>::register_token` are unprivileged, signed extrinsics that accept an arbitrary user-supplied fee asset and forward it through `swap_fee_asset_and_burn` → `swap_and_burn`, which calls `T::Swap::swap_exact_tokens_for_tokens` with the `amount_out_min` parameter hardcoded to `None`, despite the trait explicitly supporting slippage protection via `Option<Balance>`. The resulting `ether_gained`, whatever the AMM pool returns with zero floor, is burned and forwarded verbatim as the tip/registration `amount` recorded on BridgeHub.

## Finding Description
`swap_and_burn` at `bridges/snowbridge/pallets/system-frontend/src/lib.rs` lines 290-317 calls `T::Swap::swap_exact_tokens_for_tokens(who, swap_path, tip_amount, None, who, true)`, explicitly passing `None` for the minimum-output guard. The `Swap` trait (`substrate/frame/asset-conversion/src/swap.rs` lines 33-50) documents that `amount_out_min` exists precisely so callers can bound acceptable slippage and revert if the swap underdelivers — this pallet opts out of that protection entirely. Both `add_tip` (`ensure_signed`, line 261) and `register_token` (for any non-root/non-`Here` origin, line 225) route arbitrary caller-supplied fee assets through this path via `swap_fee_asset_and_burn` (lines 372-404). The unguarded `ether_gained` is then burned via `burn_for_teleport` and passed as the `amount` field in the `EthereumSystemCall::AddTip`/`RegisterToken` XCM `Transact` payload sent to BridgeHub, where it is trusted verbatim to increment the `Tips` storage (relayer reward) or registration deposit amount.

## Impact Explanation
Any signed caller can burn a large `fee_amount` of an arbitrary asset while the recorded downstream value (`ether_gained`) is disproportionately small if the pool is thin or briefly imbalanced, resulting in loss of user funds relative to the intended incentive/registration value and underpriced relayer work that can degrade bridge message processing — both align with the accepted impact categories (permanent value loss and public underpriced work).

## Likelihood Explanation
High: the `None` minimum is unconditionally hardcoded, so the missing protection is always present regardless of pool depth; triggering it only requires a signed account and an existing Swap-config'd pool for the fee asset/Ether pair, both part of the pallet's normal intended usage.

## Recommendation
Add an explicit minimum-out parameter to `swap_and_burn` (e.g., derived from a quoted price with a caller- or protocol-defined max slippage tolerance) and pass it as `Some(min_amount)` to `T::Swap::swap_exact_tokens_for_tokens`, causing the extrinsic to fail atomically if the achieved output falls below that bound, rather than silently accepting and forwarding whatever the AMM returns.

## Proof of Concept
1. Configure a `pallet_asset_conversion` pool for `(tip_asset_location, ether_location)` with low liquidity or a skewed ratio.
2. Call `EthereumSystemFrontend::add_tip(origin, message_id, Asset { id: tip_asset_location, fun: Fungible(large_amount) })`.
3. `swap_and_burn` executes `T::Swap::swap_exact_tokens_for_tokens(..., None, ...)` at `bridges/snowbridge/pallets/system-frontend/src/lib.rs` line 305, returning an arbitrarily small `ether_gained` for the large input with no revert.
4. `burn_for_teleport` burns only the small `ether_gained` while the user's full `large_amount` tip asset was consumed by the swap.
5. The BridgeHub `Tips` storage (or registration amount) records only the small `ether_gained`, demonstrating most of the user's value was lost with no corresponding relayer incentive or registered amount increase.

### Citations

**File:** substrate/frame/asset-conversion/src/swap.rs (L33-50)
```rust
	/// Swap exactly `amount_in` of asset `path[0]` for asset `path[last]`.
	/// If an `amount_out_min` is specified, it will return an error if it is unable to acquire
	/// the amount desired.
	///
	/// Withdraws the `path[0]` asset from `sender`, deposits the `path[last]` asset to `send_to`,
	/// respecting `keep_alive`.
	///
	/// If successful, returns the amount of `path[last]` acquired for the `amount_in`.
	///
	/// This operation is expected to be atomic.
	fn swap_exact_tokens_for_tokens(
		sender: AccountId,
		path: Vec<Self::AssetKind>,
		amount_in: Self::Balance,
		amount_out_min: Option<Self::Balance>,
		send_to: AccountId,
		keep_alive: bool,
	) -> Result<Self::Balance, DispatchError>;
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

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L301-308)
```rust
			let ether_gained = T::Swap::swap_exact_tokens_for_tokens(
				who.clone(),
				swap_path,
				tip_amount,
				None, // No minimum amount required
				who,
				true,
			)?;
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
