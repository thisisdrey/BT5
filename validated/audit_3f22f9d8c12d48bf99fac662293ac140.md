### Title
`swap_and_burn` in the Snowbridge System Frontend pallet performs an AMM swap with no slippage protection, allowing sandwich attacks that drain tip/fee value - (File: `bridges/snowbridge/pallets/system-frontend/src/lib.rs`)

### Summary
The Snowbridge System Frontend pallet swaps a user-supplied tip/fee asset into Ether through `pallet_asset_conversion`'s AMM pool before burning the Ether for teleportation to Ethereum. The swap call explicitly passes `None` for the minimum-output parameter — i.e. no slippage protection is applied to a call that is fully attacker-triggerable and whose price is determined by the current AMM pool reserves, exactly the missing-guard pattern described in the external report (`pool.burn`/AMM operation executed with no `amountOutMin`/slippage check).

### Finding Description
`Pallet::swap_and_burn` calls: [1](#0-0) 

```rust
fn swap_and_burn(
    origin: Location,
    tip_asset_location: Location,
    ether_location: Location,
    tip_amount: u128,
) -> Result<u128, DispatchError> {
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
    ...
}
```

`T::Swap` is `pallet_asset_conversion`'s `Swap` implementation, whose `swap_exact_tokens_for_tokens`/`do_swap_exact_tokens_for_tokens` supports an `amount_out_min` guard exactly like Uniswap's slippage check: [2](#0-1) 

That guard exists in the pallet, but `swap_and_burn` deliberately opts out of it by passing `None`. This function is invoked from `swap_fee_asset_and_burn`, which is on the path of `build_register_token_call`/tip-related public extrinsics (`register_token`, `add_tip`) that any unprivileged, unauthenticated-origin XCM/extrinsic caller can trigger with an arbitrary `tip_asset_location` and `tip_amount`: [3](#0-2) 

Because the swap executes against the live AMM pool reserves (`pallet_asset_conversion::Pools`) at the moment of dispatch, and the AMM pool itself is a permissionless, publicly swappable pool (any account can call `swap_exact_tokens_for_tokens`/`swap_tokens_for_exact_tokens` on it beforehand), an attacker can sandwich the `swap_and_burn` call:
1. Front-run: attacker swaps a large amount into the pool to skew the tip-asset/Ether price against the victim.
2. Victim's `register_token`/`add_tip` executes `swap_and_burn` with `amount_out_min = None`, receiving far less Ether than the fair-price amount for their `tip_amount`.
3. Back-run: attacker reverses their swap, capturing the price-impact spread as profit, extracted from the value the victim intended to convert to Ether and burn/teleport.

This differs from the `pallet_asset_conversion` swap extrinsics reachable directly by users (which correctly expose and enforce `amount_out_min`); the vulnerability is specifically that the System Frontend pallet's internal caller bypasses the existing slippage-check mechanism entirely.

### Impact Explanation
The corrupted value is `ether_gained` — the amount of Ether registered/burned for the caller's cross-chain operation (asset registration deposit or delivery tip). An attacker who is a fully unprivileged, ordinary chain user (no validator/collator/relayer/governance role required) can systematically extract value from any account that calls the tip/registration path, causing that account to receive less Ether than fair value for its `tip_amount` — a direct value-conservation violation ("theft ... or unbacked mint/unlock", "public underpriced work") without needing malicious infrastructure roles, matching the required-impact gate. Because `ether_gained` also determines what is burned/teleported and used to pay for delivery/registration on the Ethereum side, chronic underpricing can also cause registration/tip operations to under-fund their intended remote fee, degrading bridge processing.

### Likelihood Explanation
Likelihood is high in any deployment where the tip asset and Ether trade through a live, public, low/medium-liquidity AMM pool (as is the design intent here — `set_up_eth_and_dot_pool`/`create_pools_on_ah` helpers throughout the Snowbridge integration tests confirm this pool is meant to be a normal public pool). Any account can observe a pending `register_token`/`add_tip` transaction and sandwich it using ordinary, permissionless `swap_exact_tokens_for_tokens` calls against the same pool — no special assumptions (malicious relayer/validator/collator/governance) are needed, unlike what the Gate explicitly excludes.

### Recommendation
Do not silently disable slippage protection in `swap_and_burn`. Either:
- Add an explicit, caller-supplied `min_ether_out` parameter to `register_token`/`add_tip` (and any other public entry point that triggers `swap_and_burn`), threaded through to `swap_exact_tokens_for_tokens`'s `amount_out_min`, or
- Compute a conservative `amount_out_min` internally from a recent on-chain quote (`quote_price_exact_tokens_for_tokens`) with a bounded tolerance, and reject the swap if the realized output falls outside that tolerance.

### Proof of Concept
1. Deploy/observe the WND↔Ether (or tip-asset↔Ether) pool used by `set_up_eth_and_dot_pool` with modest reserves.
2. Attacker watches the transaction pool for a `register_token`/`add_tip` call from a victim with a known `tip_amount`.
3. Attacker submits `AssetConversion::swap_exact_tokens_for_tokens` swapping a large amount of the tip asset into Ether to shift the pool price unfavorably for the victim's coming swap (front-run).
4. Victim's extrinsic executes, internally calling `swap_and_burn`, which calls `T::Swap::swap_exact_tokens_for_tokens(..., None, ...)` — succeeding regardless of how bad the realized price is, since no `amount_out_min` is enforced (compare with the enforced check at `substrate/frame/asset-conversion/src/lib.rs:997-1002`).
5. Attacker submits a reverse swap (back-run) to restore the price and realize profit equal to the price-impact extracted from the victim's swap.
6. Victim's `ether_gained` (and thus the value burned/teleported on their behalf) is measurably lower than the fair-market amount for their `tip_amount`, while the attacker's balance increases correspondingly — demonstrating unbacked value extraction with a fully public, unprivileged attack path.

### Citations

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

**File:** substrate/frame/asset-conversion/src/lib.rs (L987-1002)
```rust
		) -> Result<T::Balance, DispatchError> {
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
