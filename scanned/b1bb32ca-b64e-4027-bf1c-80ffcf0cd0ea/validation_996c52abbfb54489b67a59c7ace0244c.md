## Analysis

The sfrxETH bug's core invariant is: **a volatile AMM/vault "preview" quote is computed once, locked into a downstream value that gets committed to (a signature or a charged amount), and re-evaluated later at a different state — the mismatch either breaks the transaction or silently changes settlement.**

The closest local analog in `polkadot-sdk--030` is in `pallet_asset_conversion_tx_payment`'s `SwapAssetAdapter`, which pays transaction fees in a non-native asset by swapping through `pallet-asset-conversion`'s AMM pools using two *separate*, state-dependent `QuotePrice` calls taken at two different points in the extrinsic's lifecycle (pre-dispatch `withdraw_fee` vs. post-dispatch `correct_and_deposit_fee`), exactly the "preview-then-settle" pattern from the report.

### Title
Fee-refund quote (`quote_price_exact_tokens_for_tokens`) recomputed post-dispatch on a pool the extrinsic itself can move, causing silent, unrecoverable overpayment when the refund swap can no longer be quoted - (File: `substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs`)

### Summary
`SwapAssetAdapter::withdraw_fee` locks in an asset amount (`asset_fee`) by quoting the AMM pool at pre-dispatch time via `S::quote_price_tokens_for_exact_tokens`, then withdraws and swaps exactly that amount for the native fee asset [1](#0-0) . After the dispatched call executes, `correct_and_deposit_fee` re-quotes the *same* pool with `S::quote_price_exact_tokens_for_tokens` to convert any unspent fee back into the user's asset, and — critically — treats a failed/degraded quote as "no refund" rather than as an error:

```
let refund_asset_amount =
    S::quote_price_exact_tokens_for_tokens(A::get(), asset_id.clone(), refund_amount, true)
        // No refund given if it cannot be swapped back.
        .unwrap_or(Zero::zero());
``` [2](#0-1) 

### Finding Description
Between the pre-dispatch quote (`withdraw_fee`) and the post-dispatch quote (`correct_and_deposit_fee`), the dispatched call itself executes with full access to the same `pallet-asset-conversion` pool that is being used for fee conversion — because it is a public pool any signed account can trade against, including in the very same pool used for the transaction's fee asset. A user (or any interleaved extrinsic in the block) can drain, dust, or heavily skew that pool's reserves as part of the dispatched call (e.g. a `swap_exact_tokens_for_tokens` on the fee-asset/native pair, or `remove_liquidity`), such that by the time `correct_and_deposit_fee` runs, `quote_price_exact_tokens_for_tokens` returns `None` — which happens whenever the pool is empty, or the computed `amount_out` exceeds `T::Assets::reducible_balance` for the target asset [3](#0-2) .

When that happens, `refund_asset_amount` becomes zero and the code takes the "cannot swap back" branch, keeping the *entire* pre-withdrawn `fee_paid` (computed for the conservatively estimated weight/fee) with **no refund at all**, even though the corrected (actual) fee may be much smaller [4](#0-3) . This is the same broken invariant as the sfrxETH bug: a value (`asset_fee`) is fixed based on a volatile preview taken at one point in time (`quote_price_tokens_for_exact_tokens`), but the corresponding "unwind"/settlement quote (`quote_price_exact_tokens_for_tokens`) is taken from a different, attacker/self-influenced pool state, and the mismatch is resolved by silently discarding value rather than failing safely or preserving the user's entitlement.

Unlike the sfrxETH case (which just reverts, a DoS), here the outcome is worse: the transaction still succeeds (`ensure!` on the withdraw-side already passed atomically), but the refund path degrades to zero refund, so the payer permanently loses the difference between the pre-charged `asset_fee` and the true corrected fee — pocketed by `OU::on_unbalanceds` (fee/tip destination) instead of returned to the payer.

### Impact Explanation
This breaks the "conserve value and settle exactly once to the rightful beneficiary and amount" invariant for transaction-fee accounting: the payer is charged based on a stale/volatile AMM quote and, upon re-quoting after their own call has moved the pool, receives zero refund instead of the correct one, resulting in an unbacked/silent transfer of value from the payer to the fee-destination (`OU`). Any runtime that configures `SwapAssetAdapter` (asset-conversion fee payment) is affected; this is a first-class FRAME pallet available for runtime configuration, not test-only code.

### Likelihood Explanation
No privileged actor, relayer, or validator collusion is required — a normal signed user submitting a single extrinsic that (a) pays fees in a non-native asset and (b) dispatches a call that trades in/against the exact fee-conversion pool (a pattern entirely reachable through public `pallet-asset-conversion` extrinsics like `swap_exact_tokens_for_tokens` or `remove_liquidity`) can trigger the zero-refund branch. It is also naturally triggerable without malice whenever pool liquidity is thin and any other extrinsic in the same block shifts the reserves between the fee-withdrawal and fee-correction quotes.

### Recommendation
`correct_and_deposit_fee` should not silently drop the refund when `quote_price_exact_tokens_for_tokens` fails. Instead:
- Track the refund as a debt/claim that can be settled asynchronously (e.g. queued and retried), or
- Fail the whole extrinsic (revert) when the refund cannot be honored, similar to the sfrxETH mitigation of binding the maximum acceptable amount and re-validating rather than defaulting to a lossy branch, or
- Compute the refund using the same pool-state snapshot used at withdrawal time (or a bounded worst-case) instead of a fresh post-dispatch quote, so that self-inflicted or third-party pool movement cannot erase the user's refund entitlement.

### Proof of Concept
1. Configure a runtime with `pallet_asset_conversion_tx_payment::SwapAssetAdapter` for `OnChargeAssetTransaction`, and create an AMM pool between the native asset `A` and asset `X` with thin liquidity.
2. Submit one extrinsic: `ChargeAssetTxPayment::from(tip, Some(X))` covering a call `AssetConversion::swap_exact_tokens_for_tokens([X, A], large_amount, min_out, attacker, false)` that drains most of asset `A` (or `X`) out of that same pool.
   - `withdraw_fee` quotes and withdraws `asset_fee` of `X` at the pre-call reserves [5](#0-4) .
3. The dispatched call executes, draining the pool.
4. `post_dispatch_details` computes `actual_fee < fee_paid`, so a refund is due; `correct_and_deposit_fee` calls `quote_price_exact_tokens_for_tokens(A, X, refund_amount, true)`, which now returns `None` because reserves are empty/insufficient [3](#0-2) .
5. `refund_asset_amount` becomes `0`; the "cannot swap back" branch fires, and the entire `fee_paid` (in asset `A`) is handed to `OU` with zero refunded to the payer in `X` [2](#0-1) , despite the corrected fee being materially lower than what was withdrawn.

### Citations

**File:** substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs (L119-176)
```rust
	fn withdraw_fee(
		who: &T::AccountId,
		_call: &T::RuntimeCall,
		_dispatch_info: &DispatchInfoOf<<T>::RuntimeCall>,
		asset_id: Self::AssetId,
		fee: Self::Balance,
		_tip: Self::Balance,
	) -> Result<Self::LiquidityInfo, TransactionValidityError> {
		if asset_id == A::get() {
			// The `asset_id` is the target asset, we do not need to swap.
			let fee_credit = F::withdraw(
				asset_id.clone(),
				who,
				fee,
				Precision::Exact,
				Preservation::Preserve,
				Fortitude::Polite,
			)
			.map_err(|_| InvalidTransaction::Payment)?;

			return Ok((fee_credit, fee));
		}

		// Quote the amount of the `asset_id` needed to pay the fee in the asset `A`.
		let asset_fee =
			S::quote_price_tokens_for_exact_tokens(asset_id.clone(), A::get(), fee, true)
				.filter(|asset_fee| !asset_fee.is_zero())
				.ok_or(InvalidTransaction::Payment)?;

		// Withdraw the `asset_id` credit for the swap.
		let asset_fee_credit = F::withdraw(
			asset_id.clone(),
			who,
			asset_fee,
			Precision::Exact,
			Preservation::Preserve,
			Fortitude::Polite,
		)
		.map_err(|_| InvalidTransaction::Payment)?;

		let (fee_credit, change) = match S::swap_tokens_for_exact_tokens(
			vec![asset_id, A::get()],
			asset_fee_credit,
			fee,
		) {
			Ok((fee_credit, change)) => (fee_credit, change),
			Err((credit_in, _)) => {
				defensive!("Fee swap should pass for the quoted amount");
				let _ = F::resolve(who, credit_in).defensive_proof("Should resolve the credit");
				return Err(InvalidTransaction::Payment.into());
			},
		};

		// Since the exact price for `fee` has been quoted, the change should be zero.
		ensure!(change.peek().is_zero(), InvalidTransaction::Payment);

		Ok((fee_credit, asset_fee))
	}
```

**File:** substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs (L259-277)
```rust
		// refund is non zero and `who`'s fee `asset_id` is not the target asset.

		// check if the refund amount can be swapped back into `who`'s fee `asset_id`.
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
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L1536-1561)
```rust
			let (balance1, balance2) = Self::get_reserves(asset1.clone(), asset2.clone()).ok()?;

			if balance1.is_zero() {
				return None;
			}

			let amount_out = if include_fee {
				let fee = Self::pool_fee_for(&asset1, &asset2).ok()?;
				Self::get_amount_out(fee, &amount, &balance1, &balance2).ok()?
			} else {
				Self::quote(&amount, &balance1, &balance2).ok()?
			};

			// Small inputs can round output to zero due to integer division.
			if amount_out.is_zero() {
				return None;
			}

			// Swap withdrawals from pools use `keep_alive=true` (Preserve). Use the same
			// preservation level to determine the actual withdrawable amount.
			let max_output = T::Assets::reducible_balance(asset2, &pool_account, Preserve, Polite);
			if amount_out > max_output {
				return None;
			}

			Some(amount_out)
```
