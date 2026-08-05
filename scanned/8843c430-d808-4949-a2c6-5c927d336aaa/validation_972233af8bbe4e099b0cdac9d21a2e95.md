Found the direct analog in `SwapFirstAssetTrader::refund_weight`.

### Title
No slippage protection when swapping refunded weight fees back to the original asset - (File: `cumulus/primitives/utility/src/lib.rs`)

### Summary
`SwapFirstAssetTrader::refund_weight` swaps unused fee credit (held in `Target` asset) back into the asset the user originally paid with, calling `SwapCredit::swap_exact_tokens_for_tokens` with `amount_out_min` hardcoded to `None` — the exact same pattern as the Pendle report's `minPtOut = 0`: an "exact-in" swap performed with zero minimum-output protection.

### Finding Description
`SwapFirstAssetTrader::buy_weight` swaps a user's arbitrary payment asset for `Target` using `SwapCredit::swap_tokens_for_exact_tokens`, accumulating the `Target`-denominated fee in `self.total_fee` [1](#0-0) .

When unused weight is later refunded, `refund_weight` extracts the corresponding portion of `total_fee` and swaps it back into the user's original asset via:
```rust
let refund = match SwapCredit::swap_exact_tokens_for_tokens(
    vec![Target::get(), refund_swap_asset],
    refund,
    None,
) {
``` [2](#0-1) 

The trait signature for `swap_exact_tokens_for_tokens` explicitly exists to let the caller bound minimum output via `amount_out_min: Option<Self::Balance>` [3](#0-2) . `pallet-asset-conversion`'s implementation enforces this bound only when `Some` is supplied — with `None`, `do_swap_exact_credit_tokens_for_tokens` skips the `ProvidedMinimumNotSufficientForSwap` check entirely and accepts whatever `amount_out` the AMM curve yields at execution time [4](#0-3) . Unlike `quote_weight`, which does call `quote_price_tokens_for_exact_tokens` to compute an expected rate before charging [5](#0-4) , `refund_weight` never quotes a price nor passes any computed minimum before executing the actual refund swap — it passes `None` directly.

This is materially different from `SwapAssetAdapter::correct_and_deposit_fee` in the tx-payment pallet, which computes `refund_asset_amount` via `quote_price_exact_tokens_for_tokens` first and passes `Some(refund_asset_amount)` as the minimum [6](#0-5)  — showing that the codebase's own established pattern for this exact operation (refunding swapped fees) is to quote-then-bound, which `SwapFirstAssetTrader::refund_weight` fails to do.

### Impact Explanation
`SwapFirstAssetTrader` is a `WeightTrader` intended for use in parachain XCM configurations where `pallet-asset-conversion` (or an equivalent `SwapCredit` implementation) backs the pool used to pay execution fees in non-native assets. Because the refund leg is executed with no floor on output, any pool-price movement between the `buy_weight` swap and the `refund_weight` swap — including price impact from the trader's own preceding swap, or from any other swap processed against the same pool within the same block/extrinsic batch — is passed through unchecked. An attacker who can influence AMM reserves for the `Target`/`refund_swap_asset` pair (e.g., via their own legitimate swaps against the shared pool in the same block, which does not require any privileged or malicious-infrastructure role) can cause the refund conversion to execute at an arbitrarily bad rate, silently reducing the value returned to XCM message senders without any error being raised (the swap simply succeeds with less output — no `ProvidedMinimumNotSufficientForSwap` is possible since no minimum was given).

### Likelihood Explanation
Likelihood is high for chains/configs that enable `SwapFirstAssetTrader` with a live AMM (this is a shipped, documented, testable component in `cumulus-primitives-utility`), and the refund path runs on essentially every XCM message that overestimates weight and pays fees in a non-`Target` asset. No privileged actor, relayer, or off-chain component is needed — normal AMM trading activity from unrelated users is sufficient to move the price between the two swaps.

### Recommendation
Compute an expected/minimum output for the refund swap before executing it, mirroring the pattern already used in `SwapAssetAdapter::correct_and_deposit_fee`: call `QuotePrice::quote_price_exact_tokens_for_tokens(Target::get(), refund_swap_asset, refund_amount, true)` to obtain an expected amount, then pass `Some(expected_amount)` (or a tolerance-adjusted value) as `amount_out_min` to `swap_exact_tokens_for_tokens` instead of `None`. On quote failure or if the resulting minimum can't be met, fall back to not refunding rather than accepting an unbounded-slippage swap.

### Proof of Concept
1. Configure a runtime with `SwapFirstAssetTrader<Target, AssetConversion, ...>` as (part of) the `WeightTrader`, backed by a live `pallet-asset-conversion` pool for `Target`/`AssetX`.
2. Send an XCM message that pays fees in `AssetX`, overestimating the weight required so that a nontrivial refund is due; `buy_weight` swaps `AssetX -> Target` and accumulates `total_fee`.
3. Before/while `refund_weight` executes (same block, via any other extrinsic or XCM trading against the same `Target`/`AssetX` pool), move the pool's reserves so that the `Target -> AssetX` rate is unfavorable.
4. Observe `refund_weight` call `SwapCredit::swap_exact_tokens_for_tokens(vec![Target::get(), refund_swap_asset], refund, None)` at `cumulus/primitives/utility/src/lib.rs:540-544` — the swap succeeds and returns a reduced `refund` amount with no error, since no `amount_out_min` was enforced, silently shortchanging the refund recipient.

### Citations

**File:** cumulus/primitives/utility/src/lib.rs (L469-490)
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
