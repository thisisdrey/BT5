Verified: the code in `cumulus/primitives/utility/src/lib.rs` matches the claim exactly — `refund_weight` calls `SwapCredit::swap_exact_tokens_for_tokens` with `amount_out_min` hardcoded to `None`, while `quote_weight` and the analogous `SwapAssetAdapter::correct_and_deposit_fee` compute an expected price first.This confirms `amount_out_min.map_or(true, |a| amount_out >= a)` — when `None`, the check is vacuously true (`map_or(true, ...)`), so no minimum-output enforcement occurs, exactly as claimed.

All code citations in the claim are verified accurate against this repository: `SwapFirstAssetTrader::refund_weight` at `cumulus/primitives/utility/src/lib.rs:540-544` passes `None` as `amount_out_min`, `SwapFirstAssetTrader::quote_weight` (lines 588-599) and `SwapAssetAdapter::correct_and_deposit_fee` (asset-conversion-tx-payment) both quote a price first as the established safe pattern, and `pallet-asset-conversion`'s `do_swap_exact_credit_tokens_for_tokens` (lines 1075-1109) confirms that `None` bypasses the `ProvidedMinimumNotSufficientForSwap` check entirely. [1](#0-0) [2](#0-1) [3](#0-2) 

Audit Report

## Title
No slippage protection when swapping refunded weight fees back to the original asset - (File: cumulus/primitives/utility/src/lib.rs)

## Summary
`SwapFirstAssetTrader::refund_weight` swaps unused fee credit (held in `Target` asset) back into the asset the user originally paid with, calling `SwapCredit::swap_exact_tokens_for_tokens` with `amount_out_min` hardcoded to `None`, meaning the swap executes at whatever rate the AMM offers with no floor on output. This is materially different from the codebase's own established safe pattern (`quote_weight` and `SwapAssetAdapter::correct_and_deposit_fee`), which both quote an expected price and pass it as a bound before executing the actual swap.

## Finding Description
`SwapFirstAssetTrader::buy_weight` swaps a user's payment asset into `Target` and accumulates the resulting `Target`-denominated credit in `self.total_fee`. When unused weight is later refunded, `refund_weight` extracts the corresponding portion of `total_fee` and calls `SwapCredit::swap_exact_tokens_for_tokens(vec![Target::get(), refund_swap_asset], refund, None)` at `cumulus/primitives/utility/src/lib.rs:540-544`. In `pallet-asset-conversion`'s `do_swap_exact_credit_tokens_for_tokens`, the minimum-output check is `ensure!(amount_out_min.map_or(true, |a| amount_out >= a), Error::<T>::ProvidedMinimumNotSufficientForSwap)` — when `None` is passed, `map_or(true, ...)` is vacuously satisfied and the check is skipped entirely, so any `amount_out` the pool yields at execution time is accepted. This contrasts with `quote_weight` (lines 588-599), which calls `quote_price_tokens_for_exact_tokens` before charging, and with `SwapAssetAdapter::correct_and_deposit_fee` in the tx-payment pallet, which calls `quote_price_exact_tokens_for_tokens` and passes `Some(refund_asset_amount)` as the minimum for the analogous refund-swap operation.

## Impact Explanation
Because the refund leg runs with no floor on output, any price movement in the `Target`/`refund_swap_asset` pool between the `buy_weight` swap and the `refund_weight` swap causes the refund amount actually delivered to be silently reduced, with no error raised. This is a value-conservation violation on the refund path for a shipped, documented `WeightTrader` component (`cumulus-primitives-utility`), affecting XCM message senders who overpay for weight and rely on `refund_weight` to return the unused portion in their original asset.

## Likelihood Explanation
No privileged actor is required — ordinary AMM trading activity from unrelated users against the same `Target`/`refund_swap_asset` pool within the same block is sufficient to move the price between the `buy_weight` and `refund_weight` swaps. Any chain configuration enabling `SwapFirstAssetTrader` with a live `pallet-asset-conversion` (or equivalent `SwapCredit`) pool is affected on essentially every XCM message that overestimates weight and pays fees in a non-`Target` asset.

## Recommendation
Before executing the refund swap, call `QuotePrice::quote_price_exact_tokens_for_tokens(Target::get(), refund_swap_asset, refund_amount, true)` to obtain an expected output, then pass `Some(expected_amount)` (optionally tolerance-adjusted) as `amount_out_min` instead of `None`, mirroring `SwapAssetAdapter::correct_and_deposit_fee`. If the quote fails or the minimum cannot be met, skip the refund swap rather than accepting an unbounded-slippage execution.

## Proof of Concept
1. Configure a runtime with `SwapFirstAssetTrader<Target, AssetConversion, ...>` as the `WeightTrader`, backed by a live `pallet-asset-conversion` pool for `Target`/`AssetX`.
2. Send an XCM message paying fees in `AssetX` that overestimates required weight, so `buy_weight` swaps `AssetX -> Target` and a refund becomes due.
3. Before `refund_weight` executes, submit any other swap against the same `Target`/`AssetX` pool (via a normal extrinsic) to move the `Target -> AssetX` rate unfavorably.
4. Observe `refund_weight` at `cumulus/primitives/utility/src/lib.rs:540-544` call `swap_exact_tokens_for_tokens(..., None)`, which succeeds and returns a reduced refund amount with no error, since `ProvidedMinimumNotSufficientForSwap` cannot trigger when `amount_out_min` is `None`.

### Citations

**File:** cumulus/primitives/utility/src/lib.rs (L539-544)
```rust
		let refund = self.total_fee.extract(refund_amount);
		let refund = match SwapCredit::swap_exact_tokens_for_tokens(
			vec![Target::get(), refund_swap_asset],
			refund,
			None,
		) {
```

**File:** cumulus/primitives/utility/src/lib.rs (L588-599)
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
		Ok((given_id, necessary_give).into())
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
