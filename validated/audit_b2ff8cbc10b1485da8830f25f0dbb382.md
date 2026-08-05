Audit Report

## Title
Unbounded-slippage AMM refund swap in `SwapFirstAssetTrader::refund_weight` causes silent XCM fee-refund loss - (File: cumulus/primitives/utility/src/lib.rs)

## Summary
`SwapFirstAssetTrader` is the `WeightTrader` used by Asset Hub (Rococo/Westend) and Penpal to allow XCM senders to pay execution fees in a non-native asset by swapping through `pallet_asset_conversion`. The buy-side swap in `buy_weight` is exact-output and thus tightly bounded, but the refund-side swap in `refund_weight` passes `amount_out_min = None` to `SwapCredit::swap_exact_tokens_for_tokens`, disabling all slippage protection on the leftover-fee refund.

## Finding Description
In `buy_weight`, the trader swaps the user's asset into `Target` using `SwapCredit::swap_tokens_for_exact_tokens`, bounding the input to exactly the `fee` required for the declared weight [1](#0-0) .

In `refund_weight`, the unused portion of `total_fee` (held in `Target`) is swapped back to the original fee asset via `SwapCredit::swap_exact_tokens_for_tokens(vec![Target::get(), refund_swap_asset], refund, None)`, with the third argument — `amount_out_min` — hardcoded to `None` [2](#0-1) .

The underlying `do_swap_exact_credit_tokens_for_tokens` implementation only enforces the `ProvidedMinimumNotSufficientForSwap` check `amount_out_min.map_or(true, |a| amount_out >= a)` — when `amount_out_min` is `None`, the check is a no-op and the swap proceeds unconditionally at whatever spot price the pool has at execution time [3](#0-2) . `quote_weight` is only used to estimate the buy-side price and is never consulted to bound the refund swap, so there is genuinely no floor applied on the refund output.

`SwapFirstAssetTrader` is confirmed wired in as the live `WeightTrader` in `cumulus/parachains/runtimes/assets/asset-hub-rococo/src/xcm_config.rs`, `asset-hub-westend/src/xcm_config.rs`, and `penpal/src/xcm_config.rs`, matching the claim's assertion that this is a live, in-scope code path rather than a test-only helper.

## Impact Explanation
This is a real, exploitable value-loss path on a live, permissionless code path: any XCM message that pays fees in a non-native, swappable asset and leaves unused weight triggers an AMM swap with zero slippage protection, so the fee payer can receive an arbitrarily diminished refund if the pool's price is skewed at execution time. This matches the "public underpriced work" impact pattern (uncompensated value loss to an unprivileged party via a public, unprotected AMM operation) permitted under the Polkadot SDK Impact Gate, since no privileged governance, validator, or off-chain infrastructure compromise is required — only ordinary, permissionless pool creation and swap submission.

## Likelihood Explanation
Exploitability requires a thin-liquidity pool for the refund asset pair (which is trivially arrangeable since pool creation via `pallet_asset_conversion` is permissionless) and the ability to submit a manipulating swap in the same block window as refund execution, both of which are achievable by any ordinary account with no special privileges, keys, or node/validator control. This is a moderate-likelihood, repeatable, unprivileged attack against a shipped configuration.

## Recommendation
Compute a defensible `amount_out_min` for the refund swap — e.g., via a price quote taken immediately before the swap with an acceptable tolerance — and pass `Some(min)` to `SwapCredit::swap_exact_tokens_for_tokens`, mirroring the bound already applied on the buy side (`swap_tokens_for_exact_tokens`). If no acceptable price is achievable, retain the credit in `Target` and route it through the existing `OnUnbalanced` path instead of executing an unconstrained swap.

## Proof of Concept
1. Attacker creates or identifies a low-liquidity `pallet_asset_conversion` pool for `(Target, refund_swap_asset)`.
2. Victim submits an XCM using `refund_swap_asset` to pay fees; `SwapFirstAssetTrader::buy_weight` swaps enough of it into `Target` for the declared weight via the exact-output swap.
3. Before `refund_weight` executes (same block), the attacker submits a large swap against the same pool to skew the `Target → refund_swap_asset` price.
4. `refund_weight` calls `SwapCredit::swap_exact_tokens_for_tokens(vec![Target::get(), refund_swap_asset], refund, None)`, which executes at the skewed price with no `ProvidedMinimumNotSufficientForSwap` check, returning far less `refund_swap_asset` than fair value to the victim.
5. The attacker reverses their manipulating swap, capturing the value differential the victim lost on the refund.

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

**File:** substrate/frame/asset-conversion/src/lib.rs (L1092-1097)
```rust
				let amount_out = path.last().map(|(_, a)| *a).ok_or(Error::<T>::InvalidPath)?;
				ensure!(
					amount_out_min.map_or(true, |a| amount_out >= a),
					Error::<T>::ProvidedMinimumNotSufficientForSwap
				);
				Ok((path, amount_out))
```
