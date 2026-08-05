## Analysis

I found a concrete local analog of the ERC4626Oracle spot-price manipulation pattern in `substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs`.

### Title
Transaction-fee refund quoted from post-dispatch AMM reserves lets a dispatched call manipulate its own fee refund - (File: `substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs`)

### Summary
`SwapAssetAdapter::withdraw_fee` quotes and swaps the fee-payment asset for the target asset **before** the extrinsic's call is dispatched, using `S::quote_price_tokens_for_exact_tokens` against the pool's current spot reserves. [1](#0-0) 

After the call executes, `correct_and_deposit_fee` computes the refund by quoting the reverse conversion again — `S::quote_price_exact_tokens_for_tokens(A::get(), asset_id, refund_amount, true)` — against whatever the pool reserves are **after** dispatch. [2](#0-1) 

### Finding Description
This mirrors the ERC4626Oracle bug class exactly: a price is derived from spot AMM reserves (`get_reserves`/`get_amount_out`/`get_amount_in` in `pallet-asset-conversion`) that any account can move within a single transaction/block, and that same-block manipulated spot price is then trusted to value an asset for a security-relevant calculation.

The `QuotePrice` trait's own documentation states the quote is "only guaranteed if no other swaps are made after the price is quoted and before the target swap." [3](#0-2) 

In `withdraw_fee`, the quote and swap happen back-to-back with no intervening state change, so that leg is self-consistent. [4](#0-3) 

However, `correct_and_deposit_fee` runs in `post_dispatch`, **after** the wrapped `RuntimeCall` has fully executed. If that dispatched call is (or triggers, e.g. via a batched/proxied call) `pallet-asset-conversion::swap_exact_tokens_for_tokens`, `add_liquidity`, or `remove_liquidity` on the exact pool between `A::get()` (the target/native fee asset) and the user's chosen `asset_id`, the caller can shift the pool's reserve ratio within their own extrinsic before the refund is priced. The refund-asset amount is then computed from the post-manipulation reserves rather than the reserves that existed when the fee was originally withdrawn, decoupling the refund pricing from the actual value that was withdrawn from the user moments earlier in the same transaction. [1](#0-0) [2](#0-1) 

Existing guards do not stop this: `can_deposit` only checks that the destination account can receive the computed `refund_asset_amount` — it does not check that the amount is fair relative to the pre-dispatch price. [5](#0-4) 
The subsequent `swap_exact_tokens_for_tokens(vec![A::get(), asset_id], refund, Some(refund_asset_amount))` executes against the (attacker-controlled) reserves and is expected to succeed for the quoted amount, with a `defensive!` on failure rather than a value-safety check. [6](#0-5) 

### Impact Explanation
This is an unprivileged, public-entrypoint path (any signed extrinsic that pays fees in a non-native asset via `ChargeAssetTxPayment`/`SwapAssetAdapter`). By choosing a call (or a `utility.batch`/nested call under the same extrinsic) that first tilts the `A`/`asset_id` pool reserves, the caller manipulates the price used to compute their own fee refund — a same-transaction spot-price attack analogous to the ERC4626 `previewRedeem` manipulation. In the worst case the attacker extracts more refund value than the fair market price of the fee actually saved, at the expense of the liquidity pool/its LPs, or the refund swap fails/underdelivers, corrupting settlement of the fee accounting for that extrinsic. This directly implicates the "Balances/assets ... must conserve value and settle exactly once to the rightful beneficiary and amount" pivot, since the corrected fee/refund amount can diverge from the true economic value.

### Likelihood Explanation
Requires only an ordinary signed account able to (a) hold liquidity/trade in the relevant pool and (b) submit one extrinsic whose dispatched call touches that pool before `correct_and_deposit_fee` runs — no validator, collator, relayer, or governance privilege needed. The pool state is public and mutable by any account with funds, and pools with the native asset are exactly the ones enabled for `ChargeAssetTxPayment`, so the attack surface is not hypothetical, though the exploitability is bounded by pool depth/liquidity and swap fees, which reduces likelihood of large-scale profit outside targeted low-liquidity pools.

### Recommendation
Cache and reuse the pre-dispatch quote/rate (or a TWAP-style price) for computing the refund conversion instead of re-querying `quote_price_exact_tokens_for_tokens` against post-dispatch reserves; alternatively, wrap the whole withdraw→dispatch→refund sequence so that the same pool state snapshot is used for both legs, or disallow refund swaps when the pool state has changed since withdrawal.

### Proof of Concept
1. Attacker holds asset `X` (registered as `asset_id`) and native asset `A`, with a shallow `X`/`A` pool.
2. Attacker submits an extrinsic with `ChargeAssetTxPayment` fee-asset = `X`, tip = 0, whose `RuntimeCall` is a `utility.batch` containing: (i) a large `pallet_asset_conversion::swap_exact_tokens_for_tokens` skewing the `X`/`A` reserve ratio favorably for `X`, then (ii) the attacker's real intended call.
3. `withdraw_fee` quotes/withdraws `X` for the fee pre-dispatch at the original (fair) rate.
4. The batched swap inside the dispatched call shifts reserves.
5. `correct_and_deposit_fee` computes `refund_asset_amount` via `quote_price_exact_tokens_for_tokens(A, X, refund_amount, true)` against the now-skewed reserves, yielding a refund in `X` priced at the manipulated rate rather than the rate the fee was originally paid at — extracting excess value from the pool/LPs within a single transaction.

**Note on confidence:** I could not fully trace whether `ChargeAssetTxPayment` allows arbitrary/batched calls in all shipped runtimes (this depends on `SignedExtension`/extension ordering and any `CallFilter` restricting `utility.batch` for fee-payment users), so the exact reachability of step 2 in a specific production runtime (e.g. Asset Hub) should be verified against that runtime's `BaseCallFilter`/extension configuration before treating this as fully weaponizable.

### Citations

**File:** substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs (L119-163)
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
```

**File:** substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs (L259-266)
```rust
		// refund is non zero and `who`'s fee `asset_id` is not the target asset.

		// check if the refund amount can be swapped back into `who`'s fee `asset_id`.
		let refund_asset_amount =
			S::quote_price_exact_tokens_for_tokens(A::get(), asset_id.clone(), refund_amount, true)
				// No refund given if it cannot be swapped back.
				.unwrap_or(Zero::zero());

```

**File:** substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs (L269-277)
```rust
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

**File:** substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs (L283-300)
```rust
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
			// The error should not occur since swap was quoted before.
			Err((refund, _)) => {
				defensive!(
```

**File:** substrate/frame/asset-conversion/src/swap.rs (L116-120)
```rust
/// Trait providing methods to quote swap prices between asset classes.
///
/// The quoted price is only guaranteed if no other swaps are made after the price is quoted and
/// before the target swap (e.g., the swap is made immediately within the same transaction).
pub trait QuotePrice {
```
