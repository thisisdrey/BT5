## Finding [1](#0-0) 

### Title
Unbounded-slippage AMM swap in `SwapFirstAssetTrader::refund_weight` (missing `amount_out_min`) - (File: `cumulus/primitives/utility/src/lib.rs`)

### Summary
`SwapFirstAssetTrader`, the XCM `WeightTrader` used to let users pay XCM execution fees in a non-native asset via `pallet-asset-conversion`, swaps the leftover `Target` fee credit back into the original client asset when refunding unused weight. The refund call passes `None` for `amount_out_min` to `SwapCredit::swap_exact_tokens_for_tokens`, i.e. it accepts any output amount from the AMM pool, exactly mirroring the reported `amountOutMinimum = 0` pattern.

### Finding Description
`SwapFirstAssetTrader::buy_weight` swaps the user's asset for the `Target` fee asset using `swap_tokens_for_exact_tokens` (exact output = fee), retaining any change credit in `total_fee`. When `refund_weight` is later invoked to return unused weight, it converts the surplus `Target` credit back to the original asset with: [2](#0-1) 
```
let refund = SwapCredit::swap_exact_tokens_for_tokens(
    vec![Target::get(), refund_swap_asset],
    refund,
    None,
)
```
The `amount_out_min` parameter of the `SwapCredit`/`Swap` trait exists specifically to bound slippage — as seen in the pallet implementation which enforces `amount_out >= amount_out_min` when it is `Some(..)`: [3](#0-2) 
By passing `None` here, that guard is entirely bypassed for this specific swap leg — the pool can return an arbitrarily small amount of the refund asset and the swap will still succeed, exactly the same broken invariant described in the external report (hardcoded/no floor on `amountOutMinimum`).

### Impact Explanation
If the `AssetConversion` pool's price for `(Target, refund_swap_asset)` is moved unfavorably between the time `buy_weight` locked in the fee and the time `refund_weight` executes the reverse swap (e.g. via another swap extrinsic against the same pool included in the same block, or natural price movement from other concurrent XCM/DEX activity), the refund conversion will silently accept a worse rate. Value that should be returned to the fee payer is instead captured by whoever moved the pool price, i.e. value is extracted from the protocol/user with no error raised — a direct funds-loss/underpriced-settlement outcome for users of any parachain runtime that wires up `SwapFirstAssetTrader` as its `WeightTrader` (`swap_first.rs` / `AssetHub`-style configurations using swap-based fee payment).

### Likelihood Explanation
`SwapFirstAssetTrader` is invoked automatically for any XCM message that pays fees in a non-`Target` asset — this is a fully public, unprivileged code path, not requiring any admin/governance/validator/collator role. The only conditions needed are (a) an `AssetConversion` pool exists for the fee asset and `Target`, and (b) the pool price shifts between the `buy_weight` and `refund_weight` calls for that message — achievable by any actor able to place a swap against the same pool in temporal proximity. Every other swap entry point in the pallet (`swap_exact_tokens_for_tokens` extrinsic, precompile, `buy_weight`'s own `swap_tokens_for_exact_tokens` call) correctly threads a `Some(..)` minimum/maximum bound; only this refund leg hardcodes `None`.

### Recommendation
Compute and pass an explicit `amount_out_min` for the refund swap (e.g. derived from a quoted price via `QuotePrice::quote_price_exact_tokens_for_tokens` with a bounded tolerance, as `SwapFirstAssetTrader` already depends on `QuotePrice`), and treat a swap failure due to insufficient output as a partial-refund failure (return `None`/skip refund) rather than accepting whatever amount the pool yields.

### Proof of Concept
1. Configure a runtime with `SwapFirstAssetTrader<Target, SwapCredit=pallet_asset_conversion::Pallet<T>, ...>` as `WeightTrader`.
2. Create an `AssetConversion` pool for `(Target, ClientAsset)`.
3. Submit an XCM message paying fees in `ClientAsset`; `buy_weight` swaps into `Target`, leaving `total_fee` with an excess balance to be refunded later.
4. In the same block (or before `refund_weight` runs for that message), submit an ordinary `swap_exact_tokens_for_tokens`/`swap_tokens_for_exact_tokens` extrinsic that heavily skews the `(Target, ClientAsset)` pool ratio.
5. When `refund_weight` executes `SwapCredit::swap_exact_tokens_for_tokens(vec![Target, ClientAsset], refund, None)`, it accepts the skewed rate — the refunded amount is far below fair value, with no `Error::ProvidedMinimumNotSufficientForSwap` ever triggered because `amount_out_min` was `None`.

### Citations

**File:** cumulus/primitives/utility/src/lib.rs (L538-558)
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
