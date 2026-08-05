Audit Report

## Title
`SwapFirstAssetTrader::refund_weight` performs the mandatory XCM fee-refund swap with `amount_out_min = None`, allowing unbounded slippage on the refund credit - ([File: cumulus/primitives/utility/src/lib.rs])

## Summary
`SwapFirstAssetTrader::refund_weight` converts the unused portion of a user's XCM `BuyExecution` fee (held in `Target`) back into the asset the user originally paid with, by calling `SwapCredit::swap_exact_tokens_for_tokens(vec![Target::get(), refund_swap_asset], refund, None)`. Passing `None` for `amount_out_min` disables `pallet_asset_conversion`'s only slippage guard (`ProvidedMinimumNotSufficientForSwap`), so the refund swap executes at whatever price the pool currently offers, including a price degraded by an attacker who moves the pool immediately before the refund executes. This lets an unprivileged user front-run/manipulate the relevant `pallet_asset_conversion` pool to make the automatically-triggered refund pay out materially less than owed to the message's beneficiary.

## Finding Description
In `buy_weight`, the trader swaps the user's asset into `Target` using `swap_tokens_for_exact_tokens`, where the desired output (`fee`) is fixed and risk is bounded because the input credit is capped by what the user supplied: [1](#0-0) 

In `refund_weight`, the unused portion of `self.total_fee` (denominated in `Target`) is extracted and swapped back into the asset the user paid with via `swap_exact_tokens_for_tokens`, with `amount_out_min` hard-coded to `None`: [2](#0-1) 

This is the inverse operation shape (exact-in / variable-out), which is precisely the operation type that needs a minimum-out bound, yet none is supplied. The `quote_weight` implementation shows that `SwapCredit`/`QuotePrice` is already available to the trader to compute an expected price, but it is not used to derive a floor for the refund swap: [3](#0-2) 

At the pallet level, `pallet_asset_conversion`'s swap logic only enforces the minimum-out check when the caller actually supplies `Some(min)`; passing `None` bypasses the check entirely: [4](#0-3) 

`SwapFirstAssetTrader` is not a hypothetical/unused primitive — it is wired in as the configured `WeightTrader` in live parachain runtimes, confirmed present in `cumulus/parachains/runtimes/assets/asset-hub-westend/src/xcm_config.rs`, `asset-hub-rococo/src/xcm_config.rs`, `cumulus/parachains/runtimes/testing/penpal/src/xcm_config.rs`, and `substrate/frame/staking-async/runtimes/parachain/src/xcm_config.rs`. Because it is registered as the runtime's `WeightTrader`, `refund_weight` is invoked automatically by the XCM executor for any incoming XCM program containing `BuyExecution` that leaves unused weight — this is a permissionless, unprivileged, and unconditionally reachable code path, not gated behind any origin or governance check. `pallet_asset_conversion` pools are themselves permissionless to create (`create_pool`) and trade against, so an attacker can create or thin a `(Target, refund_swap_asset)` pool, or swap against an existing one, to degrade the refund price right before a victim's refund executes.

## Impact Explanation
This falls under the "public underpriced work" / value-conservation impact category: the refund settlement is supposed to return the correct amount of the original asset back to the message's beneficiary, but with no slippage floor, the swap can settle at an arbitrarily bad price, transferring value from the rightful beneficiary to the counter-party/LPs of the manipulated pool. This is a fund-loss condition reachable by an ordinary, unprivileged user submitting or influencing an XCM message and pool state — no malicious validator, collator, compromised relayer, or leaked keys are required, matching the scoped impact gate for runtime bugs that compromise intended settlement behavior and value conservation.

## Likelihood Explanation
Medium likelihood: exploitation requires (1) a runtime that configures `SwapFirstAssetTrader` as its `WeightTrader` — confirmed true for asset-hub-westend, asset-hub-rococo, the penpal testing runtime, and the staking-async parachain runtime — and (2) an attacker able to move the specific `(Target, refund_swap_asset)` pool price in the same execution window as the victim's refund, which is achievable via a thinly-liquidated or attacker-created pool combined with a same-block/adjacent-block swap, similar to standard sandwich/price-manipulation setups against permissionless AMM pools. This does not require any privileged role, and is repeatable against any XCM message that triggers a non-trivial refund.

## Recommendation
Compute a slippage-bounded `amount_out_min` for the refund swap before calling `swap_exact_tokens_for_tokens`, e.g., by deriving an expected output from `QuotePrice::quote_price_exact_tokens_for_tokens` (mirroring the pattern already used in `quote_weight`) and applying an acceptable tolerance. If the swap cannot clear that bound, fall back to retaining the unswapped `Target` credit (e.g., treat it as a failed refund, as the `Err` branch already does) rather than accepting an unbounded price.

## Proof of Concept
1. Configure a runtime with `SwapFirstAssetTrader<Target, AssetConversion, ..>` as its `WeightTrader` (already true for asset-hub-westend/rococo and penpal).
2. Permissionlessly create/thin the `(Target, AssetX)` pool in `pallet_asset_conversion` via `create_pool`/`add_liquidity`.
3. Submit an XCM message that pays weight fees in `AssetX` via `BuyExecution`, supplying more `AssetX` than the program consumes so a refund via `refund_weight` is triggered.
4. In the same block/execution window, execute a large swap against the `(Target, AssetX)` pool to move the price unfavorably for the `Target → AssetX` direction.
5. Observe `refund_weight` at `cumulus/primitives/utility/src/lib.rs:539-544` calling `swap_exact_tokens_for_tokens(vec![Target::get(), refund_swap_asset], refund, None)` and returning far less `AssetX` than the actual value of the refunded weight fee, with no error raised since no minimum-out check exists to fail against.

### Citations

**File:** cumulus/primitives/utility/src/lib.rs (L469-489)
```rust
		let fee = WeightToFee::weight_to_fee(&weight);
		// swap the user's asset for the `Target` asset.
		let (credit_out, credit_change) = match SwapCredit::swap_tokens_for_exact_tokens(
			vec![swap_asset, Target::get()],
			credit_in,
			fee,
		) {
			Ok(a) => a,
			Err((credit_in, error)) => {
				log::trace!(
					target: "xcm::weight",
					"SwapFirstAssetTrader::buy_weight swap couldn't be done. Error was: {:?}",
					error,
				);
				// put back the taken credit
				let taken =
					AssetsInHolding::new_from_fungible_credit(id.clone(), Box::new(credit_in));
				payment.subsume_assets(taken);
				return Err((payment, XcmError::FeesNotMet));
			},
		};
```

**File:** cumulus/primitives/utility/src/lib.rs (L539-558)
```rust
		let refund = self.total_fee.extract(refund_amount);
		let refund = match SwapCredit::swap_exact_tokens_for_tokens(
			vec![Target::get(), refund_swap_asset],
			refund,
			None,
		) {
			Ok(refund_in_target) => refund_in_target,
			Err((refund, _)) => {
				// return an attempted refund back to the `total_fee`.
				let _ = self.total_fee.subsume(refund).map_err(|refund| {
					// error may occur if `total_fee.asset` differs from `refund.asset`, which does
					// not apply in this context.
					defensive!(
						"`total_fee.asset` must be equal to `refund.asset`",
						(self.total_fee.asset(), refund.asset())
					);
				});
				return None;
			},
		};
```

**File:** cumulus/primitives/utility/src/lib.rs (L588-598)
```rust
		let want_amount = WeightToFee::weight_to_fee(&weight);
		// The `give` amount required to obtain `want`.
		let necessary_give: u128 = <SwapCredit as QuotePrice>::quote_price_tokens_for_exact_tokens(
			give_fungibles_id,
			want_fungibles_id,
			want_amount,
			true, // Include fee.
		)
		.filter(|amount| *amount > 0u128.into())
		.ok_or(XcmError::FeesNotMet)?
		.into();
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
