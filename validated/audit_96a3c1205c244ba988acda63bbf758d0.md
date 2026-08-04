## Finding: Unprotected refund swap in `SwapFirstAssetTrader::refund_weight`

### Title
Sandwichable XCM weight-fee refund due to missing slippage bound in `SwapFirstAssetTrader::refund_weight` - (File: `cumulus/primitives/utility/src/lib.rs`)

### Summary
`SwapFirstAssetTrader` is a `WeightTrader` used by the XCM executor to let users pay execution fees in an asset other than the chain's native fee asset (`Target`), automatically converting via `pallet-asset-conversion`'s `SwapCredit`. When unused weight must be refunded back to the user's original asset, `refund_weight` performs the reverse swap with **no minimum-output bound**, unlike `buy_weight`, which is protected by specifying an exact `amount_out`. [1](#0-0) 

### Finding Description
In `buy_weight`, the user's asset is swapped for an exact amount of `Target` needed to cover the fee, via `swap_tokens_for_exact_tokens`, which inherently bounds the amount of the user's asset spent by `credit_in`'s balance: [2](#0-1) 

However, in `refund_weight`, the leftover `Target` asset fee is swapped back into the user's original asset using `swap_exact_tokens_for_tokens(..., refund, None)` — the `amount_out_min` parameter is hardcoded to `None`: [3](#0-2) 

Passing `None` disables the slippage guard that `pallet-asset-conversion`'s `do_swap_exact_credit_tokens_for_tokens` otherwise enforces via `ProvidedMinimumNotSufficientForSwap`: [4](#0-3) 

This is the exact analog of the reported `Voting.sol::_tryFinalize` bug: an automatic "refund to the fee payer" swap is executed on a public, price-manipulable AMM pool without any caller- or protocol-specified minimum acceptable output. The refund happens deterministically as part of XCM message-queue processing in a block, so any actor who can also submit ordinary swap extrinsics against the same `pallet-asset-conversion` pool (permissionless, no validator/collator/relayer collusion needed) can sandwich it: push the pool price against `Target -> refund_swap_asset` immediately before the message is processed, let the refund execute at the degraded price, then reverse the price move and pocket the difference.

### Impact Explanation
The fee-paying account (any XCM message sender using this trader, e.g. incoming XCM from another chain/asset via Asset Hub configurations) loses value on every refund whenever the pool can be moved, without needing a malicious collator, validator, or governance actor — only ordinary permissionless swap transactions in the same block. This is a direct fund-loss/value-extraction bug in a public dispatch/execution path (`WeightTrader` used by the XCM executor), matching the "theft… duplicate settlement or payout… without direct machine access" impact class.

### Likelihood Explanation
`SwapFirstAssetTrader` is a general-purpose, opt-in `WeightTrader` shipped in `cumulus-primitives-utility` for parachain XCM configs that want to accept arbitrary fee-payment assets convertible via `pallet-asset-conversion`. Any runtime wiring this trader with a shallow or thinly-liquid `Target`/`refund_swap_asset` pool is exploitable by anyone able to submit ordinary swap extrinsics in the same block the refund is processed — no special privileges required. Likelihood scales with how much refund value accumulates (i.e., how much unused weight is bought) and pool liquidity depth.

### Recommendation
Do not pass `None` for the refund's `amount_out_min`. Compute an acceptable minimum (e.g. via `QuotePrice::quote_price_exact_tokens_for_tokens` at call time with a bounded tolerance, or track/record the effective price used in `buy_weight` and require the refund to not be worse than that by more than an allowed tolerance), and propagate the swap error path (already handled) if the bound is not met.

### Proof of Concept
1. Configure a parachain XCM executor with `SwapFirstAssetTrader<Target, SwapCredit=AssetConversion, ...>` where `Target` and `refund_swap_asset` form a `pallet-asset-conversion` pool with attacker-accessible liquidity.
2. A user sends an XCM message that overpays weight in `refund_swap_asset`; `buy_weight` swaps enough of it into `Target` to cover the exact fee, leaving unused weight to refund later in the same message's execution.
3. An attacker, in the same block, right before message-queue processing dispatches the message, submits `AssetConversion::swap_exact_tokens_for_tokens` to push the `Target -> refund_swap_asset` price down.
4. `refund_weight` executes `SwapCredit::swap_exact_tokens_for_tokens(vec![Target, refund_swap_asset], refund, None)` at the manipulated price, giving the user far less `refund_swap_asset` back than fair value.
5. The attacker reverses their swap after the refund, restoring the price and capturing the difference extracted from the refund.

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

**File:** substrate/frame/asset-conversion/src/lib.rs (L1092-1102)
```rust
				let amount_out = path.last().map(|(_, a)| *a).ok_or(Error::<T>::InvalidPath)?;
				ensure!(
					amount_out_min.map_or(true, |a| amount_out >= a),
					Error::<T>::ProvidedMinimumNotSufficientForSwap
				);
				Ok((path, amount_out))
			};
			let (path, amount_out) = match inspect_path(credit_in.asset()) {
				Ok((p, a)) => (p, a),
				Err(e) => return Err((credit_in, e)),
			};
```
