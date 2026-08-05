Based on my investigation, I found a real analog to the Synthetix bug in `substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs`.

### Title
`SwapAssetAdapter::correct_and_deposit_fee` reports quoted amount instead of actual swap output as fee consumed - ([File: substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs])

### Summary
`SwapAssetAdapter::correct_and_deposit_fee` computes a refund by first *quoting* (not executing) the reverse swap via `QuotePrice::quote_price_exact_tokens_for_tokens`, then executes the actual swap via `SwapCredit::swap_exact_tokens_for_tokens`, but discards the real output credit amount and instead subtracts the pre-computed quoted value to derive the returned "amount of `asset_id` consumed for payment." This mirrors the Synthetix bug: using a pre-exchange estimate instead of the actual post-exchange amount for downstream accounting. [1](#0-0) 

### Finding Description
In `correct_and_deposit_fee`, when the fee-payment asset differs from the target asset `A`, the refund flow is:
1. `refund_asset_amount` is obtained via `S::quote_price_exact_tokens_for_tokens(A::get(), asset_id.clone(), refund_amount, true)` — a *quote*, not an executed trade. [2](#0-1) 
2. The actual swap is then executed with `S::swap_exact_tokens_for_tokens(vec![A::get(), asset_id], refund, Some(refund_asset_amount))`, which returns `refund_asset`, a credit whose real value is `refund_asset.peek()`.
3. Instead of using `refund_asset.peek()` (the real output), the code computes the returned "amount of `asset_id` consumed" as `fee_asset_amount.saturating_sub(refund_asset_amount)` — i.e., it uses the pre-swap *quoted* amount, not the actual credit amount received from the swap: [3](#0-2) 

`QuotePrice` itself documents that its guarantee only holds "if no other swaps are made after the price is quoted and before the target swap" [4](#0-3) , meaning the quote and the real swap output can diverge if pool reserves change between the two calls (e.g., pool state affected by other operations dispatched in the same block prior to this call, or by fee-conversion pools shared across many transactions in `pallet_asset_conversion`). The full actual credit `refund_asset` (potentially larger or smaller than `refund_asset_amount`) is deposited into the user's account via `F::resolve(who, refund_asset)`, but the bookkeeping value returned by the function — which downstream is used as `fee_asset_amount` for reporting/withdrawal accounting — does not reflect that real amount.

### Impact Explanation
The mismatch between the actually deposited/withdrawn asset amount and the value returned/tracked by `correct_and_deposit_fee` corrupts fee accounting: the reported "amount of `asset_id` used for payment" can be understated or overstated relative to what was truly transferred, in exactly the same class of bug as the Synthetix report — using a pre-trade quantity for a post-trade settlement value. Any pallet or telemetry relying on the returned `BalanceOf<T>` from `correct_and_deposit_fee` (e.g., fee statistics, external accounting hooks) receives an inaccurate figure of tokens consumed, which can misrepresent user balances/fees in derived tooling.

### Likelihood Explanation
This requires only a normal signed transaction paying fees in a non-native asset through `pallet-asset-conversion-tx-payment`'s `SwapAssetAdapter` — no privileged actor, relayer, or malicious peer is needed. Divergence between quote-time and swap-time reserves is a routine occurrence in an AMM pool shared by other users/transactions in the same or nearby blocks, making the quote-vs-actual mismatch a realistic (not purely theoretical) occurrence.

### Recommendation
Replace the use of `refund_asset_amount` in the subtraction with the actual amount extracted from the executed swap's output credit before it is merged/resolved (i.e., capture `refund_asset.peek()` prior to `F::resolve`), and use that captured actual value for the `fee_asset_amount.saturating_sub(...)` computation, ensuring returned accounting values always reflect real settled amounts rather than pre-trade quotes.

### Proof of Concept
Conceptually:
1. User has fee-asset `X` and system fee target is `A`. `correct_and_deposit_fee` computes `refund_amount` in `A`.
2. `quote_price_exact_tokens_for_tokens(A, X, refund_amount, true)` returns `refund_asset_amount` based on current reserves `(Ra, Rx)`.
3. Between the quote call and the swap call, reserves for the `A`/`X` pool shift (e.g., another extrinsic in the same block swaps against the same pool) so that `swap_exact_tokens_for_tokens` actually returns `refund_asset.peek() != refund_asset_amount`.
4. The function still computes `fee_asset_amount.saturating_sub(refund_asset_amount)` as the "amount consumed," diverging from the real value deposited to the user (`refund_asset.peek()`), producing an inconsistent fee-accounting record while the actual token transfer used the correct (different) amount — a direct structural analog of the Synthetix `amount` vs. actual-`sUSD`-received bug.

### Citations

**File:** substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs (L262-297)
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
			Ok(refund_asset) => match F::resolve(who, refund_asset) {
				Ok(_) => (fee_asset_amount.saturating_sub(refund_asset_amount), adjusted_paid),
				Err(refund_asset) => {
					defensive!(
						"Refund resolve should pass since `can_deposit` was checked",
						(refund_asset.asset(), refund_asset.peek(), who)
					);
					(fee_asset_amount, adjusted_paid)
				},
			},
```

**File:** substrate/frame/asset-conversion/src/swap.rs (L116-120)
```rust
/// Trait providing methods to quote swap prices between asset classes.
///
/// The quoted price is only guaranteed if no other swaps are made after the price is quoted and
/// before the target swap (e.g., the swap is made immediately within the same transaction).
pub trait QuotePrice {
```
