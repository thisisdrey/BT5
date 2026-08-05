Audit Report

## Title
Sandwichable XCM weight-fee refund due to missing slippage bound in `SwapFirstAssetTrader::refund_weight` - (File: `cumulus/primitives/utility/src/lib.rs`)

## Summary
`SwapFirstAssetTrader::refund_weight` swaps leftover `Target` fee credit back into the user's original fee-payment asset via `SwapCredit::swap_exact_tokens_for_tokens(vec![Target::get(), refund_swap_asset], refund, None)`, passing `None` for `amount_out_min`. This disables the slippage protection that `pallet-asset-conversion` otherwise enforces, unlike `buy_weight`, which uses `swap_tokens_for_exact_tokens` and is inherently bounded by the exact fee amount required.

## Finding Description
`buy_weight` swaps the user's asset for an exact `Target` amount (`fee`) via `SwapCredit::swap_tokens_for_exact_tokens`, bounding the amount spent by the caller's `credit_in`. [1](#0-0) 

`refund_weight`, however, extracts the unused portion of `total_fee` and swaps it back to the original asset using `swap_exact_tokens_for_tokens` with `amount_out_min` hardcoded to `None`. [2](#0-1) 

In `pallet-asset-conversion`'s `do_swap_exact_credit_tokens_for_tokens`, the minimum-output check is explicitly gated on `amount_out_min` being `Some`: `ensure!(amount_out_min.map_or(true, |a| amount_out >= a), Error::<T>::ProvidedMinimumNotSufficientForSwap)`. Passing `None` unconditionally satisfies this check via `map_or(true, ...)`, so no minimum-output enforcement occurs. [3](#0-2) 

Because the pool referenced by `Target`/`refund_swap_asset` is a standard, permissionless `pallet-asset-conversion` pool, any account can submit ordinary `swap_exact_tokens_for_tokens`/`swap_tokens_for_exact_tokens` extrinsics to move the price immediately before the XCM message queue processes the refund, extract value from the unprotected refund swap, then reverse the price move — a classic sandwich attack requiring no validator, collator, or governance privilege, only ordinary public extrinsic submission in the same block.

## Impact Explanation
This directly corrupts the amount of `refund_swap_asset` returned to the fee-paying account: the "beneficiary correctness/amount" invariant for the refund payout is violated because the swap executes at an attacker-manipulated price with no protocol- or caller-specified floor. This is value extraction from a public, unprivileged, permissionless attack path (ordinary swap extrinsics against a public AMM pool) that degrades a public execution path (`WeightTrader` used by the XCM executor during message processing), matching the allowed "theft… duplicate settlement or payout" / "public underpriced work" impact class under the Polkadot SDK Impact Gate.

## Likelihood Explanation
`SwapFirstAssetTrader` is a general-purpose, opt-in `WeightTrader` present in `cumulus-primitives-utility` and referenced by parachain XCM configs (e.g. asset-hub-rococo/westend, penpal, staking-async parachain runtime). Exploitation requires only: (1) a configured pool between `Target` and the refund asset with attacker-accessible liquidity, and (2) the ability to submit ordinary swap extrinsics in the same block the refund executes — both are unprivileged, permissionless capabilities. No malicious collator/validator/governance assumption is needed, satisfying the "unprivileged external attacker using public extrinsics" requirement.

## Recommendation
Do not pass `None` for `amount_out_min` in the refund swap. Compute a bounded minimum acceptable output at refund time (e.g., via a spot/TWAP quote with an allowed tolerance, or by tracking the effective price achieved during the corresponding `buy_weight` call and requiring the refund rate not be worse than that by more than a defined tolerance), and propagate the existing error-handling path (already present) when the bound is not met.

## Proof of Concept
1. Configure `SwapFirstAssetTrader<Target, SwapCredit=AssetConversion, ...>` with a `Target`/`refund_swap_asset` pool with modest attacker-accessible liquidity.
2. A user's XCM message overpays weight in `refund_swap_asset`; `buy_weight` swaps enough into `Target` to cover the exact fee, leaving unused weight/fee credit for later refund in the same message execution.
3. An attacker submits `AssetConversion::swap_exact_tokens_for_tokens` in the same block, immediately prior to message-queue processing, to push the `Target -> refund_swap_asset` price down.
4. `refund_weight` executes `SwapCredit::swap_exact_tokens_for_tokens(vec![Target, refund_swap_asset], refund, None)` at the manipulated price, returning far less `refund_swap_asset` to the user than fair value; `do_swap_exact_credit_tokens_for_tokens`'s `ensure!` check trivially passes because `amount_out_min` is `None`.
5. The attacker reverses their swap after the refund completes, restoring pool price and capturing the extracted difference — reproducible as a unit/integration test asserting the refunded amount degrades proportionally to attacker-injected price movement with no lower bound enforced.

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

**File:** substrate/frame/asset-conversion/src/lib.rs (L1092-1096)
```rust
				let amount_out = path.last().map(|(_, a)| *a).ok_or(Error::<T>::InvalidPath)?;
				ensure!(
					amount_out_min.map_or(true, |a| amount_out >= a),
					Error::<T>::ProvidedMinimumNotSufficientForSwap
				);
```
