Confirmed: `SwapFirstAssetTrader` is actually wired in as the `WeightTrader` in real production configs (`cumulus/parachains/runtimes/assets/asset-hub-rococo/src/xcm_config.rs`, `asset-hub-westend/src/xcm_config.rs`, `penpal/src/xcm_config.rs`), so this is a live, in-scope code path, not a test-only helper.

### Title
Unbounded-slippage AMM refund swap in `SwapFirstAssetTrader::refund_weight` causes silent XCM fee-refund loss - (File: cumulus/primitives/utility/src/lib.rs)

### Summary
`SwapFirstAssetTrader` is the `WeightTrader` used by Asset Hub (Rococo/Westend) and Penpal to let XCM senders pay execution fees in a non-native asset by swapping it through `pallet_asset_conversion`. When buying weight it correctly swaps with an exact target fee via `swap_tokens_for_exact_tokens`, but when refunding unused weight it calls `SwapCredit::swap_exact_tokens_for_tokens` with `amount_out_min = None`, i.e. no minimum-output/slippage protection at all.

### Finding Description
In `buy_weight` [1](#0-0) , the swap that takes fees from the user's asset into the pool's `Target` asset uses `swap_tokens_for_exact_tokens`, which is bounded by the exact `fee` amount needed — no slippage risk beyond what is strictly required.

However, in `refund_weight` the leftover `Target` credit is swapped back into the original fee asset with: [2](#0-1) 
```rust
let refund = self.total_fee.extract(refund_amount);
let refund = match SwapCredit::swap_exact_tokens_for_tokens(
    vec![Target::get(), refund_swap_asset],
    refund,
    None,
) {
```
The third argument is the `amount_out_min` parameter of `SwapCredit::swap_exact_tokens_for_tokens` [3](#0-2) , and passing `None` disables the `ProvidedMinimumNotSufficientForSwap` check entirely (see `do_swap_exact_credit_tokens_for_tokens`, where the check is skipped whenever `amount_out_min` is `None`) [4](#0-3) .

This means the AMM-priced refund swap executes at *whatever* the pool's spot price is at the moment the XCM is processed, with zero floor on the output. `quote_weight` only estimates the buy-side price via `quote_price_tokens_for_exact_tokens` and is never consulted for the refund path, so there is no on-chain "expected" value that the actual refund is checked against — directly mirroring the report's core defect: no slippage/deadline bound on a price-sensitive AMM operation.

### Impact Explanation
Because this is a live `WeightTrader` in Asset Hub / Penpal XCM configs, every inbound/outbound XCM message that pays fees in a non-native, swappable asset and leaves unused weight triggers this unbounded swap. An attacker who can influence the pool's reserves in the same block (e.g., by executing their own large swap on the same `pallet_asset_conversion` pool immediately before the refunded XCM is processed by the executor, which is entirely public and unprivileged) can move the spot price so the refund swap returns a token amount far below fair value. The victim (the XCM's fee payer) silently receives a diminished — potentially near-zero — refund, while the value differential flows to the pool's liquidity providers/arbitrageurs. This is value loss with no protection on a public, permissionless entrypoint (any account can create AMM pools and submit XCM/asset-conversion transactions in the same block as pending XCM processing), matching the "public underpriced work" / uncompensated value-loss pattern from the seed report.

### Likelihood Explanation
Likelihood is moderate: it requires (1) a shallow/thin liquidity pool for the refund asset pair (easily arranged by an attacker who creates a low-liquidity pool themselves, since pool creation is permissionless), and (2) the ability to place a manipulating swap transaction in the same block window as the refund execution, which is achievable by any account submitting ordinary extrinsics — no validator/collator/relayer/admin compromise needed. No governance or privileged action is required anywhere in this path.

### Recommendation
Compute an `amount_out_min` for the refund swap (e.g., via `QuotePrice::quote_price_exact_tokens_for_tokens` immediately before swapping, with an acceptable tolerance) and pass `Some(min)` instead of `None` to `SwapCredit::swap_exact_tokens_for_tokens`, mirroring the bound already applied on the buy-side (`swap_tokens_for_exact_tokens`). If no acceptable price can be achieved, keep the credit in `Target` and drop it via the existing `OnUnbalanced` path rather than accepting an unconstrained swap.

### Proof of Concept
1. Attacker creates (or identifies) a low-liquidity `pallet_asset_conversion` pool for `(Target, refund_swap_asset)`.
2. Victim submits an XCM (or Asset Hub call producing `BuyExecution` fee payment) using `refund_swap_asset` to pay fees; `SwapFirstAssetTrader::buy_weight` swaps enough of it into `Target` for the declared weight.
3. Before the corresponding `refund_weight` is executed (same block, since XCM execution and any attacker extrinsic land in the same block via `on_idle`/message queue processing), attacker submits a large `swap_exact_tokens_for_tokens`/`swap_tokens_for_exact_tokens` against the same pool to skew the `Target`→`refund_swap_asset` price.
4. When `refund_weight` runs, `SwapCredit::swap_exact_tokens_for_tokens(vec![Target, refund_swap_asset], refund, None)` executes at the skewed price with no `ProvidedMinimumNotSufficientForSwap` check, returning far less `refund_swap_asset` than fair value.
5. Attacker reverses their manipulating swap in the same or next block, capturing the value the victim lost on the refund, at zero cost beyond swap fees and capital they get back.

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

**File:** substrate/frame/asset-conversion/src/swap.rs (L43-50)
```rust
	fn swap_exact_tokens_for_tokens(
		sender: AccountId,
		path: Vec<Self::AssetKind>,
		amount_in: Self::Balance,
		amount_out_min: Option<Self::Balance>,
		send_to: AccountId,
		keep_alive: bool,
	) -> Result<Self::Balance, DispatchError>;
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
