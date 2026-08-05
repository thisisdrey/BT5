## Analysis

The external report's core issue is a swap executed with **zero minimum output** (`amountOutMin = 0`), which disables slippage protection and lets an attacker sandwich the swap to siphon value. Searching `polkadot-sdk` for the closest analogous pattern — a public AMM swap invoked without slippage bound as part of protocol logic (not user-supplied) — surfaces `SwapFirstAssetTrader::refund_weight` in `cumulus/primitives/utility/src/lib.rs`.

### Title
Missing slippage protection in `SwapFirstAssetTrader::refund_weight` XCM fee-refund swap - (`cumulus/primitives/utility/src/lib.rs`)

### Summary
`SwapFirstAssetTrader` is a `WeightTrader` used by the XCM executor to let users pay XCM execution fees in a non-native asset by swapping it (via `pallet_asset_conversion`'s `SwapCredit`) into a `Target` fee asset. When unused weight is refunded, `refund_weight` swaps the surplus `Target` credit back into the asset the user originally paid with — but it calls `SwapCredit::swap_exact_tokens_for_tokens` with `amount_out_min` hard-coded to `None`, i.e. no minimum-output check at all. [1](#0-0) 

### Finding Description
`refund_weight` computes `refund_amount` from `WeightToFee::weight_to_fee(&weight)` and then swaps it through the public asset-conversion pool identified by `[Target::get(), refund_swap_asset]`:

```rust
let refund = self.total_fee.extract(refund_amount);
let refund = match SwapCredit::swap_exact_tokens_for_tokens(
    vec![Target::get(), refund_swap_asset],
    refund,
    None,
) { ... };
```

`pallet_asset_conversion::Pallet::do_swap_exact_credit_tokens_for_tokens` (the concrete `SwapCredit` implementation used at runtime) only enforces the minimum-output check when `amount_out_min` is `Some`: [2](#0-1) 

Because `refund_weight` always passes `None`, the swap accepts whatever output the pool's current reserves yield at execution time, with zero floor. This mirrors exactly the `FujiVaultFTM`/`SwapperFTM` pattern in the external report, where `amountOutMin = 0` neutralized slippage checks on a swap performed automatically by protocol code rather than by the end user, and where the pallet's own `Swap`/`SwapCredit` trait already supports the `Some(min)` slippage path (as seen used correctly elsewhere, e.g. the tx-payment refund logic which quotes a price first via `quote_price_exact_tokens_for_tokens` and passes `Some(refund_asset_amount)`): [3](#0-2) 

That contrasts directly with `SwapFirstAssetTrader::refund_weight`, which has all the data needed (the `QuotePrice` bound is already present on `SwapCredit` in this struct) to quote an expected output and enforce a floor, but does not do so.

### Impact Explanation
Any parachain runtime wiring `SwapFirstAssetTrader` as its `WeightTrader` for XCM fee payment in non-native assets is exposed: an unprivileged actor can manipulate the reserves of the public `[Target, refund_swap_asset]` liquidity pool immediately around the block/extrinsics that trigger XCM message execution and refund, forcing the refund swap to execute at an unfavorable price and capturing the difference through the pool (classic sandwich). This drains value from the pallet's own fee-refund credit — i.e. protocol/user funds meant to be returned to the XCM fee payer — without needing a malicious validator, collator, or relayer; only ordinary swap transactions against the same public pool are required.

### Likelihood Explanation
The pool used (`Target`/`refund_swap_asset`) is a standard public `pallet_asset_conversion` pool that anyone can trade in. Because `refund_weight` is invoked deterministically as part of normal XCM execution (whenever unused weight needs refunding after fee payment in a non-Target asset), an attacker can predict/observe when refunds are likely to occur (e.g., after submitting their own XCM message) and pre/post-trade the pool around it. No privileged access or off-chain conditions are needed beyond normal DEX trading capability.

### Recommendation
Quote the expected refund amount via `QuotePrice::quote_price_exact_tokens_for_tokens` (already available on the `SwapCredit` bound, exactly as done in `pallet-asset-conversion-tx-payment`'s `correct_and_deposit_fee`) and pass it as `Some(min_expected)` to `swap_exact_tokens_for_tokens`, falling back to skipping/deferring the refund if no acceptable quote exists, mirroring the pattern already used in `substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs`.

### Proof of Concept
1. Configure a runtime with `SwapFirstAssetTrader<Target, SwapCredit, ...>` as `WeightTrader`, backed by `pallet_asset_conversion` pools for `Target`/`AssetX`.
2. User submits an XCM message paying fees in `AssetX`; `buy_weight` swaps `AssetX` → `Target` for the exact estimated fee.
3. Attacker, observing the pending XCM execution, trades heavily against the `[Target, AssetX]` pool to skew reserves just before the block executes the refund.
4. Execution completes with less-than-estimated weight used, triggering `refund_weight`, which calls `swap_exact_tokens_for_tokens(vec![Target, AssetX], refund, None)` — no floor is enforced, so the swap executes at the attacker-skewed price.
5. Attacker reverses their trade in the same or next block, pocketing the spread; the refund credited back to the original fee-payer (or the amount retained by the trader/`OnUnbalanced`) is less than the fair-market value, with the difference extracted via the pool.

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

**File:** substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs (L262-287)
```rust
		let refund_asset_amount =
			S::quote_price_exact_tokens_for_tokens(A::get(), asset_id.clone(), refund_amount, true)
				// No refund given if it cannot be swapped back.
				.unwrap_or(Zero::zero());

		// `fee_paid` cannot be swapped back into `who`'s fee `asset_id` or the refund amount cannot
		// be deposited into `who`'s fee `asset_id`, exit without refund.
		if refund_asset_amount.is_zero() ||
			!matches!(
				F::can_deposit(asset_id.clone(), who, refund_asset_amount, Provenance::Extant),
				DepositConsequence::Success
			) {
			let (tip, fee) = fee_paid.split(tip);
			OU::on_unbalanceds(Some(fee).into_iter().chain(Some(tip)));
			return Ok(fee_asset_amount);
		}

		// swap the refund amount back into `who`'s fee `asset_id`.

		let (refund, adjusted_paid) = fee_paid.split(refund_amount);

		let (fee_asset_amount, adjusted_paid) = match S::swap_exact_tokens_for_tokens(
			vec![A::get(), asset_id],
			refund,
			Some(refund_asset_amount),
		) {
```
