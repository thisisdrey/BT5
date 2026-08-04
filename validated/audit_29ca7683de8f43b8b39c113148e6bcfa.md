## Analysis

The external report's core broken invariant is: **a token swap executes without any lower bound on the output amount, so price movement/slippage can silently give the caller a wrong (too-low) amount, and nothing in the code path validates it.**

This exact pattern exists in `polkadot-sdk` in `SwapFirstAssetTrader::refund_weight`. [1](#0-0) 

### Title
Unbounded-slippage swap in `SwapFirstAssetTrader::refund_weight` allows XCM fee refunds to be silently under-delivered — (File: `cumulus/primitives/utility/src/lib.rs`)

### Summary
`SwapFirstAssetTrader` is a `WeightTrader` used by the XCM executor to let users pay delivery/execution fees in a non-native fungible asset by swapping it (via `pallet_asset_conversion`'s `SwapCredit`) into the chain's `Target` fee asset. When unused weight must be refunded (`refund_weight`), the trader swaps the surplus `Target` asset back into the asset the user originally paid with. That reverse swap calls `SwapCredit::swap_exact_tokens_for_tokens(vec![Target::get(), refund_swap_asset], refund, None)` with `amount_out_min` hard-coded to `None`, i.e. with no slippage/minimum-output protection at all — the exact bug class described in the LOGLAB-22 report.

### Finding Description
`pallet_asset_conversion`'s `SwapCredit::swap_exact_tokens_for_tokens` trait explicitly supports an `Option<Balance>` `amount_out_min` to guard against unfavorable pool pricing: [2](#0-1) 

Every other in-repo caller of this trait method that matters for value-correctness (e.g. `SingleAssetExchangeAdapter::exchange_asset`, used by the XCM `ExchangeAsset` instruction) supplies a concrete minimum: [3](#0-2) 

But `SwapFirstAssetTrader::refund_weight` passes `None`: [4](#0-3) 

`do_swap_exact_credit_tokens_for_tokens` only rejects the swap on a minimum when one is provided — with `None` the check is skipped entirely and whatever `amount_out` the AMM curve produces at that instant is accepted unconditionally: [5](#0-4) 

So the refunded value received back into the `AssetsInHolding` (and ultimately deposited/returned to the XCM originator) is whatever the pool state dictates at execution time, with zero floor. Contrast this with `buy_weight` in the same struct, which correctly uses `swap_tokens_for_exact_tokens` with an exact target `fee` amount — only the refund path lost its bound.

Existing guards do not stop this: `do_swap_exact_credit_tokens_for_tokens`'s `ensure!(amount_out_min.map_or(true, |a| amount_out >= a), ...)` is a no-op when the caller passes `None`, and there is no other check anywhere in `refund_weight` or the `WeightTrader::refund_weight` XCM-executor call site that inspects the resulting refunded amount against an expectation.

### Impact Explanation
This directly affects "public underpriced work" / value-conservation guarantees for cross-chain fee handling: any XCM message that overpays weight in a non-`Target` fungible asset and triggers a refund will have that refund executed through an AMM swap with no minimum-output enforcement. If the relevant pool is shallow, has been temporarily skewed (e.g., by preceding swaps within the same block/message batch, since Substrate blocks are not front-run-only — sequencing within a single block by the same XCM executor instance is fully deterministic and repeatable), the user can receive an arbitrarily small amount of their original asset back instead of the expected refund — a silent, protocol-level fund loss on every chain wiring up `SwapFirstAssetTrader` as its `WeightTrader`. This is a real, repository-level implementation bug in shared cross-consensus fee-charging logic that ships as part of `cumulus/primitives/utility`, usable by any parachain runtime.

### Likelihood Explanation
Likelihood is elevated because: (1) `SwapFirstAssetTrader` is a general-purpose, reusable utility explicitly designed for allowing arbitrary fungible assets to pay XCM fees — any runtime configuring it inherits the bug without additional code; (2) the vulnerable path (`refund_weight`) executes automatically whenever there is unused weight after `buy_weight`, which is the common case for any overestimated `BuyExecution` weight limit — this needs no privileged actor, governance action, or malicious peer, only an ordinary unprivileged user sending a normal XCM message with a fee overpayment in a non-target asset.

### Recommendation
Pass an explicit `Some(minimum_acceptable_amount)` to the reverse swap in `refund_weight`, computed the same way `buy_weight`'s swap is bounded (e.g., derive an expected minimum via `QuotePrice::quote_price_exact_tokens_for_tokens` before swapping, or track/require the amount originally converted from that asset during `buy_weight` as the floor). If the swap cannot meet that minimum, keep the refund in the `Target` asset (or return `None`/skip the refund) rather than accepting an unconstrained AMM output.

### Proof of Concept
1. Configure a `Target`/`AssetA` pool in `pallet_asset_conversion` with shallow liquidity.
2. Construct an XCM message with `BuyExecution { fees: (AssetA, large_amount), weight_limit }` where the declared weight substantially exceeds actual weight consumed by the program (trivial: send a message with a large explicit weight limit and a short program).
3. `SwapFirstAssetTrader::buy_weight` swaps enough `AssetA` into `Target` to cover `fee = WeightToFee::weight_to_fee(&weight)`.
4. After executing the (short) program, the executor calls `refund_weight` for the large unused weight portion; this calls `SwapCredit::swap_exact_tokens_for_tokens(vec![Target, AssetA], refund, None)`.
5. Before/around this refund swap, drain or skew the `Target`/`AssetA` pool with other swaps (also permitted operations, no privilege required) inside the same block prior to this message's execution, so the reverse-swap curve returns far less `AssetA` than the fair-value refund would be.
6. The XCM originator receives a refund far smaller than `WeightToFee::weight_to_fee(&weight)` worth of `AssetA`, with no error and no floor enforced — demonstrating unconditional value loss via the unbounded `None` `amount_out_min`.

### Citations

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

**File:** substrate/frame/asset-conversion/src/swap.rs (L85-97)
```rust
	/// Swap exactly `credit_in` of asset `path[0]` for asset `path[last]`.  If `amount_out_min` is
	/// provided and the swap can't achieve at least this amount, an error is returned.
	///
	/// On a successful swap, the function returns the `credit_out` of `path[last]` obtained from
	/// the `credit_in`. On failure, it returns an `Err` containing the original `credit_in` and the
	/// associated error code.
	///
	/// This operation is expected to be atomic.
	fn swap_exact_tokens_for_tokens(
		path: Vec<Self::AssetKind>,
		credit_in: Self::Credit,
		amount_out_min: Option<Self::Balance>,
	) -> Result<Self::Credit, (Self::Credit, DispatchError)>;
```

**File:** polkadot/xcm/xcm-builder/src/asset_exchange/single_asset_adapter/adapter.rs (L107-114)
```rust
		let (credit_out, maybe_credit_change) = if maximal {
			// If `maximal`, then we swap exactly `credit_in` to get as much of `want_asset_id` as
			// we can, with a minimum of `want_amount`.
			let credit_out = match <AssetConversion as SwapCredit<_>>::swap_exact_tokens_for_tokens(
				vec![swap_asset, want_asset_id],
				credit_in,
				Some(want_amount),
			) {
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
