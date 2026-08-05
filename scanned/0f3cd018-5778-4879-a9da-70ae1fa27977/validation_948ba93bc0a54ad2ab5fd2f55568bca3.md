Found a solid local analog: `swap_and_burn` in `SnowbridgeSystemFrontend` hardcodes `None` for the minimum-output parameter when swapping a user-supplied fee/tip asset for Ether via `pallet_asset_conversion::Swap::swap_exact_tokens_for_tokens`, exactly mirroring the Orca `decreaseLiquidity(amount, 0, 0)` pattern from the report.

### Title
Missing slippage protection in Snowbridge `swap_and_burn` lets tip/fee swaps be sandwiched for zero output guarantee - (File: `bridges/snowbridge/pallets/system-frontend/src/lib.rs`)

### Summary
The `snowbridge-pallet-system-frontend` pallet swaps a caller-supplied fee/tip asset for Ether through `pallet_asset_conversion::Pallet::swap_exact_tokens_for_tokens` in order to burn the resulting Ether for teleportation to Ethereum. The call passes `None` as `amount_out_min`, disabling the pallet's built-in slippage check entirely, even though the swap output directly determines the amount of Ether burned/teleported (used to pay Ethereum-side execution costs or relayer tips).

### Finding Description
`Pallet::swap_and_burn` invokes the AMM swap with no minimum-output bound: [1](#0-0) 

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

This is called from `swap_fee_asset_and_burn`, reachable from the two signed/public extrinsics `register_token` and `add_tip`: [2](#0-1) [3](#0-2) 

`pallet_asset_conversion`'s `do_swap_exact_tokens_for_tokens` only enforces the `ProvidedMinimumNotSufficientForSwap` check when `amount_out_min` is `Some`: [4](#0-3) 

Passing `None` disables this guard entirely — the swap accepts whatever `amount_out` the AMM reserves happen to produce at execution time, no matter how low. This is functionally identical to the reported Orca bug's `decreaseLiquidity(amount, 0, 0)` call: the "minimum acceptable output" is effectively unbounded downward.

### Impact Explanation
Because `add_tip` and `register_token` are plain signed extrinsics, an attacker (or the same MEV infrastructure referenced in the original report, e.g. searcher bots) can observe a pending transaction that will trigger this swap, and sandwich it: front-run by moving the pool's price against the swap direction, let the victim's swap execute at the worsened price (which passes with zero enforced floor since `amount_out_min` is `None`), then back-run to restore price and capture the difference. The result is `ether_gained` — the amount burned/teleported for Ethereum-side execution — being reduced far below fair value, directly reducing execution/tip funding intended for the user's cross-chain message and transferring that value to the attacker. This causes real loss of user funds through a public, unprivileged, underpriced-value path in the live Snowbridge BridgeHub code, matching the "theft or unbacked mint" / "public underpriced work" impact classes in scope.

### Likelihood Explanation
`add_tip` requires only `ensure_signed`, no special origin or governance action, and `register_token` is open to any origin with the standard `EnsureOriginWithArg` check on `asset_id` (not on the fee asset or amount) — any user can trigger the vulnerable code path with attacker-observable, attacker-timable parameters (`tip_amount`, `fee_asset`). No malicious validator/collator/relayer/admin is required; a bot watching the public transaction pool suffices, consistent with the accepted "public underpriced work" pivot.

### Recommendation
Do not pass `None` for `amount_out_min`. Compute an acceptable minimum (e.g., via `AssetConversionApi::quote_price_exact_tokens_for_tokens` with a caller- or pallet-configured slippage tolerance) and pass `Some(min_out)` to `swap_exact_tokens_for_tokens`, propagating a `SlippageExceeded`-style error (mirroring `ProvidedMinimumNotSufficientForSwap`) back to the extrinsic caller on failure. Optionally expose an explicit `min_ether_out` parameter on `add_tip`/`register_token` so users can set their own tolerance, analogous to the report's suggested `withdraw(uint64 amount, uint64 minA, uint64 minB)` fix.

### Proof of Concept
1. Attacker monitors the mempool/block builder for a pending `add_tip(message_id, asset)` (or `register_token`) call with a sizeable `tip_amount`/`fee_asset` in a non-Ether asset.
2. Attacker submits a transaction that swaps a large amount of Ether-out of the same pool (or the same asset pair) immediately before the victim's transaction, shifting the pool ratio unfavorably for the victim's upcoming swap.
3. Victim's transaction executes `swap_and_burn` → `swap_exact_tokens_for_tokens(..., None, ...)`; since no floor is enforced, the swap succeeds even though `ether_gained` is far lower than the fair-market value of `tip_amount`.
4. Attacker submits a back-run transaction restoring the pool ratio, extracting the price difference as profit.
5. The victim's tip/fee is effectively partially stolen: `ether_gained` (and thus the Ether burned for Ethereum execution) is reduced, while the attacker profits from the sandwich — no code path in `pallet_asset_conversion` or `snowbridge-pallet-system-frontend` prevents this because the slippage guard was disabled via `None`.

### Citations

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L225-252)
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

			let ether_gained = if origin_location.is_here() {
				// Root origin/location does not pay any fees/tip.
				0
			} else {
				Self::swap_fee_asset_and_burn(origin_location.clone(), fee_asset)?
			};

			let call = Self::build_register_token_call(
				origin_location.clone(),
				asset_location,
				metadata,
				ether_gained,
			)?;

			Self::send_transact_call(origin_location, call)
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

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L296-308)
```rust
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
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L988-1002)
```rust
			ensure!(amount_in > Zero::zero(), Error::<T>::ZeroAmount);
			if let Some(amount_out_min) = amount_out_min {
				ensure!(amount_out_min > Zero::zero(), Error::<T>::ZeroAmount);
			}

			Self::validate_swap_path(&path)?;
			let path = Self::balance_path_from_amount_in(amount_in, path)?;

			let amount_out = path.last().map(|(_, a)| *a).ok_or(Error::<T>::InvalidPath)?;
			if let Some(amount_out_min) = amount_out_min {
				ensure!(
					amount_out >= amount_out_min,
					Error::<T>::ProvidedMinimumNotSufficientForSwap
				);
			}
```
