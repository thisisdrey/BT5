Audit Report

## Title
`SwapFirstAssetTrader::refund_weight` swaps XCM fee refunds through the AMM with `amount_out_min = None`, exposing users to unbounded slippage/sandwiching - (File: `cumulus/primitives/utility/src/lib.rs`)

## Summary
`SwapFirstAssetTrader::refund_weight` swaps the unspent portion of `total_fee` back into the original fee-payment asset via `SwapCredit::swap_exact_tokens_for_tokens(vec![Target::get(), refund_swap_asset], refund, None)`, passing `None` as `amount_out_min`, which disables all slippage protection for this leg. This is in contrast to `buy_weight`, which bounds its swap using `swap_tokens_for_exact_tokens` with an exact-output cap, making the missing bound in the refund path an inconsistency that directly exposes fee-payers to value loss from adverse AMM price movement.

## Finding Description
`SwapCredit::swap_exact_tokens_for_tokens` is implemented by `pallet_asset_conversion::Pallet<T>::do_swap_exact_credit_tokens_for_tokens`, which only enforces the `ProvidedMinimumNotSufficientForSwap` floor when the caller supplies `Some(min)`: [1](#0-0) . `SwapFirstAssetTrader::refund_weight` calls this exact function with `None` at [2](#0-1) , unconditionally accepting whatever `amount_out` the pool state yields for the given `refund` credit amount at the moment of execution. This differs from `buy_weight`, which bounds the initial swap via an exact-output call `swap_tokens_for_exact_tokens` capped by `credit_in` at [3](#0-2) , and from `quote_weight`, which already has access to `QuotePrice::quote_price_tokens_for_exact_tokens` for computing expected amounts at [4](#0-3) . No downstream code re-validates the swapped-out amount before it is wrapped into `AssetsInHolding` and returned as the refund at [5](#0-4) , so an unprivileged actor moving the same pool's reserves via ordinary `pallet_asset_conversion` swap extrinsics interleaved before/around the refund's execution can cause the fee-payer to receive a reduced amount of the original payment asset with no on-chain floor to prevent it.

## Impact Explanation
This is value loss/underpricing of a public, unprivileged-triggerable code path (automatic XCM fee-refund accounting) that runs whenever a parachain configures `SwapFirstAssetTrader` and an XCM message pays fees in a non-`Target` asset with unused weight to refund. The corrupted value is the refund amount credited back to the fee-payer via `refund_swap_asset` — it can be silently reduced below its fair value because the swap accepts any AMM-quoted output. This matches the "public underpriced work" / value-loss impact category, since the fee-payer cannot set their own slippage tolerance for this leg (it happens automatically inside XCM weight accounting, not as a user-bounded extrinsic).

## Likelihood Explanation
Medium. No privileged access is required — any account can submit ordinary, permissionless `pallet_asset_conversion::swap_exact_tokens_for_tokens` calls against the same `Target`/`refund_swap_asset` pool to shift reserves unfavorably in the same block as the refund. The precondition ("an XCM message pays fees in a non-`Target` asset via this trader and has unused weight to refund") is a normal, frequently-occurring execution path for parachains that configure this `WeightTrader`, not an edge case.

## Recommendation
Do not pass `None` for `amount_out_min` in `refund_weight`. Compute a bound using `QuotePrice::quote_price_exact_tokens_for_tokens` immediately before the swap (mirroring the pattern already used in `quote_weight`), apply a configurable tolerance, and pass `Some(min_expected)` into `SwapCredit::swap_exact_tokens_for_tokens`. If the swap would violate this floor, retain the credit in `total_fee` (as already done on `Err`) instead of accepting an unbounded-loss trade.

## Proof of Concept
1. Configure a parachain runtime with `SwapFirstAssetTrader<Target, pallet_asset_conversion::Pallet<Runtime>, WeightToFee, ..>` as an XCM `WeightTrader`, backed by a live `Target`/`OtherAsset` pool in `pallet_asset_conversion`.
2. Submit an XCM message paying execution fees in `OtherAsset` with more than the actually-consumed weight, so `buy_weight` swaps `OtherAsset -> Target` and a nonzero `weight` remains to be refunded via `refund_weight`.
3. Around the block containing this XCM message's execution, submit ordinary `pallet_asset_conversion::swap_exact_tokens_for_tokens` calls that shift the `Target`/`OtherAsset` pool reserves unfavorably (e.g., dump `Target` into the pool to depress its price relative to `OtherAsset`).
4. Observe that `refund_weight`'s call at `cumulus/primitives/utility/src/lib.rs:540-544` executes and returns a smaller-than-expected amount of `OtherAsset`, since `amount_out_min = None` disables the `ProvidedMinimumNotSufficientForSwap` check in `do_swap_exact_credit_tokens_for_tokens` (`substrate/frame/asset-conversion/src/lib.rs:1075-1097`), confirming the refunded value to the original fee-payer is silently reduced with no on-chain floor.

### Citations

**File:** substrate/frame/asset-conversion/src/lib.rs (L1075-1097)
```rust
		pub(crate) fn do_swap_exact_credit_tokens_for_tokens(
			path: Vec<T::AssetKind>,
			credit_in: CreditOf<T>,
			amount_out_min: Option<T::Balance>,
		) -> Result<CreditOf<T>, (CreditOf<T>, DispatchError)> {
			let amount_in = credit_in.peek();
			let inspect_path = |credit_asset| {
				ensure!(
					path.first().map_or(false, |a| *a == credit_asset),
					Error::<T>::InvalidPath
				);
				ensure!(!amount_in.is_zero(), Error::<T>::ZeroAmount);
				ensure!(amount_out_min.map_or(true, |a| !a.is_zero()), Error::<T>::ZeroAmount);

				Self::validate_swap_path(&path)?;
				let path = Self::balance_path_from_amount_in(amount_in, path)?;

				let amount_out = path.last().map(|(_, a)| *a).ok_or(Error::<T>::InvalidPath)?;
				ensure!(
					amount_out_min.map_or(true, |a| amount_out >= a),
					Error::<T>::ProvidedMinimumNotSufficientForSwap
				);
				Ok((path, amount_out))
```

**File:** cumulus/primitives/utility/src/lib.rs (L470-475)
```rust
		// swap the user's asset for the `Target` asset.
		let (credit_out, credit_change) = match SwapCredit::swap_tokens_for_exact_tokens(
			vec![swap_asset, Target::get()],
			credit_in,
			fee,
		) {
```

**File:** cumulus/primitives/utility/src/lib.rs (L539-544)
```rust
		let refund = self.total_fee.extract(refund_amount);
		let refund = match SwapCredit::swap_exact_tokens_for_tokens(
			vec![Target::get(), refund_swap_asset],
			refund,
			None,
		) {
```

**File:** cumulus/primitives/utility/src/lib.rs (L560-561)
```rust
		let refund = AssetsInHolding::new_from_fungible_credit(refund_asset.id, Box::new(refund));
		Some(refund)
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
