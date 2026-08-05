All claim details are confirmed by direct inspection of the code.

Audit Report

## Title
Unbounded-slippage AMM swap in XCM fee-refund path allows value extraction from asset-conversion pools - ([File: cumulus/primitives/utility/src/lib.rs])

## Summary
`SwapFirstAssetTrader::buy_weight` correctly protects the fee-payer by requesting an exact output amount (`fee`) via `SwapCredit::swap_tokens_for_exact_tokens` [1](#0-0) , but `refund_weight` swaps the unused fee back into the original asset using `SwapCredit::swap_exact_tokens_for_tokens` with `amount_out_min` hard-coded to `None` [2](#0-1) . This removes all slippage protection on the refund leg, letting anyone who can manipulate the pool's price (via ordinary permissionless swap extrinsics) siphon value out of the refund that should go back to the XCM fee-payer.

## Finding Description
`refund_weight` extracts `refund_amount` from `self.total_fee` (denominated in `Target`) and swaps it into `refund_swap_asset` via `SwapCredit::swap_exact_tokens_for_tokens(vec![Target::get(), refund_swap_asset], refund, None)` [2](#0-1) . This call flows into `pallet_asset_conversion`'s `do_swap_exact_tokens_for_tokens`, where the `amount_out_min` check is entirely skipped when `None` is supplied: `if let Some(amount_out_min) = amount_out_min { ensure!(...) }` [3](#0-2) . Consequently, `refund_weight` accepts whatever output the constant-product pool computes at execution time, no matter how unfavorable.

Because `AssetConversion::swap_exact_tokens_for_tokens` and `swap_tokens_for_exact_tokens` are public, permissionless, signed extrinsics operating against the same pool, an attacker can move the pool price against the `Target -> refund_swap_asset` leg immediately before the refund executes, then reverse the trade afterward, extracting the difference between the fair-value refund and the manipulated-price refund. This is confirmed live in the runtime configs: `SwapFirstAssetTrader` is wired into `xcm_config.rs` for Asset Hub Rococo, Asset Hub Westend, Penpal, and the staking-async parachain template. In contrast, `buy_weight` correctly protects the user by requesting an exact output amount rather than accepting any price [1](#0-0) , showing the asymmetry is a genuine oversight rather than an intentional design choice.

## Impact Explanation
This is a public, unprivileged, underpriced-swap issue reachable during ordinary XCM message processing on live Asset Hub runtimes. It matches the "public underpriced work" impact category: value that should be refunded to the fee-payer in the correct asset amount can instead be captured by an attacker manipulating the AMM pool price, resulting in the fee-payer receiving a wrong (understated) refund amount — the exact corrupted value is the `refund` credit amount computed by `SwapCredit::swap_exact_tokens_for_tokens` at line 540–544 of `cumulus/primitives/utility/src/lib.rs`.

## Likelihood Explanation
The attack requires only the ability to submit ordinary signed `AssetConversion::swap_exact_tokens_for_tokens` / `swap_tokens_for_exact_tokens` extrinsics against the same pool used for the fee asset — a normal, permissionless action requiring no privileged access, no malicious validator/collator, and no leaked keys. Feasibility is bounded by pool liquidity depth and the attacker's ability to influence extrinsic ordering relative to the refund's execution within a block, which is a realistic (if not guaranteed) capability for transaction-pool participants, making this moderately likely and repeatable given XCM messages that overpay fees in non-native assets are routine.

## Recommendation
Do not call `SwapCredit::swap_exact_tokens_for_tokens` with `amount_out_min = None` in `refund_weight`. Compute and pass a safe minimum output — e.g., by recording the effective exchange rate used in `buy_weight` (or querying `quote_price_exact_tokens_for_tokens`) and deriving a corresponding `amount_out_min` for the refund leg — and fail closed (skip the reverse swap, returning the refund in `Target` asset) if a safe minimum cannot be established, mirroring the protection already present in `buy_weight`.

## Proof of Concept
1. Configure a parachain using `SwapFirstAssetTrader` (as Asset Hub Westend/Rococo, Penpal, and the staking-async parachain template do) with an asset-conversion pool for `(NonNativeAsset, Target)`.
2. A user submits an XCM message overpaying weight fees in `NonNativeAsset`; `buy_weight` swaps into `Target` with a proper `Some(fee)` bound (line 471-475).
3. Before the block processes the XCM's `refund_weight` call, an attacker submits `AssetConversion::swap_exact_tokens_for_tokens` trades against the same pool to push the `Target -> NonNativeAsset` price down.
4. `refund_weight` executes `SwapCredit::swap_exact_tokens_for_tokens(vec![Target, NonNativeAsset], refund, None)` (line 540-544) and accepts whatever reduced output the manipulated pool returns, since `do_swap_exact_tokens_for_tokens` skips the `amount_out_min` check entirely when `None` (line 988-1002 in `substrate/frame/asset-conversion/src/lib.rs`).
5. The attacker reverses their initial trade, capturing the value that should have been refunded to the fee payer.

### Citations

**File:** cumulus/primitives/utility/src/lib.rs (L469-475)
```rust
		let fee = WeightToFee::weight_to_fee(&weight);
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
