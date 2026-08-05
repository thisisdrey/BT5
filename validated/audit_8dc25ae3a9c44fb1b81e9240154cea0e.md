Audit Report

## Title
Fee-refund swap in `pallet-asset-conversion-tx-payment` uses post-call spot pool price, letting an attacker self-sandwich fee/refund swaps and drain LP funds - (File: `substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs`)

## Summary
`SwapAssetAdapter` executes a real AMM swap in `withdraw_fee` before the user's `call` is dispatched, and a second real AMM swap in `correct_and_deposit_fee` after the `call` has executed, both priced from `pallet_asset_conversion`'s live spot reserves via `quote_price_tokens_for_exact_tokens`/`quote_price_exact_tokens_for_tokens`. Because the dispatched `call` sits between the two swaps and can freely mutate the very pool used for fee payment (e.g. via public `swap_exact_tokens_for_tokens`, `add_liquidity`, `remove_liquidity`), an attacker can distort the pool's reserve ratio between the debit and refund legs and extract value from the pool's other liquidity providers within a single atomic transaction.

## Finding Description
`ChargeAssetTxPayment::prepare` calls `withdraw_fee`, which invokes `SwapAssetAdapter::withdraw_fee` [1](#0-0) . This quotes and executes an actual state-mutating swap against the live pool reserves via `quote_price_tokens_for_exact_tokens` and `swap_tokens_for_exact_tokens` [2](#0-1) .

After the runtime executive dispatches the user's `call` (which can be any public extrinsic, including swaps/liquidity operations on the same pool), `post_dispatch_details` calls `correct_and_deposit_fee` [3](#0-2) , which quotes the refund purely from whatever reserves exist *at that moment* via `quote_price_exact_tokens_for_tokens` and then executes `swap_exact_tokens_for_tokens` [4](#0-3) . Both quotes read only the pool account's current balances via `get_reserves` [5](#0-4) , with no mechanism tying the refund rate to the rate used at fee-debit time, and the pallet's own doc explicitly flags this staleness risk without providing any cross-swap guard [6](#0-5) . The `amount_out_min` slippage bound used in the refund swap is itself derived from the already-manipulated post-call reserves, so it provides no protection against the manipulation that occurred between the two swaps.

## Impact Explanation
This falls under the "Balances, assets ... pools ... must conserve value and settle exactly once to the rightful beneficiary and amount" invariant. An unprivileged, unauthenticated attacker who pays fees in a manipulable `asset_id`/native pool can structure their own dispatched `call` to skew that pool's reserves between the pre-dispatch debit swap and the post-dispatch refund swap, causing the refund swap to settle at a distorted rate and extract value from other liquidity providers of that pool, using only public extrinsics and a signed transaction with `ChargeAssetTxPayment`.

## Likelihood Explanation
Exploitability requires a fee-asset pool with liquidity depth small enough relative to the attacker's manipulation capability, and a dispatched `call` that measurably shifts that pool's reserves in the same transaction — both trivially achievable via `swap_exact_tokens_for_tokens`/`add_liquidity`/`remove_liquidity`, all public entry points on `pallet_asset_conversion`. The entire attack is self-contained within one transaction: no cross-block timing, front-running, or third-party cooperation is required.

## Recommendation
- Cache the pre-dispatch quoted rate (or the reserves observed during `withdraw_fee`) and reuse it, or bound the refund conversion by it, so the two swaps cannot diverge based on state mutated by the call being paid for.
- Alternatively, disallow or specially guard fee-asset pools that are also touched by the dispatched call in the same transaction, or use a time-weighted average price instead of two independent spot reads for the debit/refund pair.

## Proof of Concept
1. Attacker provides thin/imbalanced liquidity to an `asset_id`/native pool via `pallet_asset_conversion::create_pool` + `add_liquidity`, alongside other independent LPs.
2. Attacker submits a signed extrinsic using `ChargeAssetTxPayment::from(tip, Some(asset_id))`, where the inner `call` is a large `swap_exact_tokens_for_tokens`/`remove_liquidity` on that same pool.
3. `prepare()` triggers `withdraw_fee`, swapping asset_id for the exact native fee at the pre-manipulation rate [7](#0-6) .
4. The executive dispatches the attacker's `call`, skewing the pool's reserve ratio.
5. `post_dispatch_details()` triggers `correct_and_deposit_fee`, quoting and swapping the native refund back into `asset_id` at the now-skewed rate [8](#0-7) , returning more `asset_id` than the fair pre-manipulation rate would allow, at the expense of the other pool LPs.

### Citations

**File:** substrate/frame/transaction-payment/asset-conversion-tx-payment/src/lib.rs (L336-339)
```rust
			Val::Charge { tip, who, fee } => {
				// Mutating call of `withdraw_fee` to actually charge for the transaction.
				let (_fee, initial_payment) = self.withdraw_fee(&who, call, info, fee)?;
				Ok(Pre::Charge { tip, who, initial_payment, weight: self.weight(call) })
```

**File:** substrate/frame/transaction-payment/asset-conversion-tx-payment/src/lib.rs (L402-410)
```rust
				let converted_fee = T::OnChargeAssetTransaction::correct_and_deposit_fee(
					&who,
					info,
					&actual_post_info,
					actual_fee,
					tip,
					asset_id.clone(),
					already_withdrawn,
				)?;
```

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

**File:** substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs (L210-323)
```rust
	fn correct_and_deposit_fee(
		who: &T::AccountId,
		_dispatch_info: &DispatchInfoOf<<T>::RuntimeCall>,
		_post_info: &PostDispatchInfoOf<<T>::RuntimeCall>,
		corrected_fee: Self::Balance,
		tip: Self::Balance,
		asset_id: Self::AssetId,
		already_withdrawn: Self::LiquidityInfo,
	) -> Result<BalanceOf<T>, TransactionValidityError> {
		// (fee_paid: Credit in target `A` asset, fee_asset_amount: Balance in `asset_id`
		// consumed to obtain the target `A` asset)
		let (fee_paid, fee_asset_amount) = already_withdrawn;
		let refund_amount = fee_paid.peek().saturating_sub(corrected_fee);

		// nothing to refund or the account was removed by to the dispatched function.
		if refund_amount.is_zero() || F::total_balance(asset_id.clone(), who).is_zero() {
			let (tip, fee) = fee_paid.split(tip);
			OU::on_unbalanceds(Some(fee).into_iter().chain(Some(tip)));
			return Ok(fee_asset_amount);
		}

		// The `asset_id` is the target `A` asset, we do not need to swap.
		if asset_id == A::get() {
			let (refund, adjusted_paid) = fee_paid.split(refund_amount);

			let (fee_asset_amount, adjusted_paid) = match F::resolve(who, refund) {
				Ok(_) => (adjusted_paid.peek(), adjusted_paid),
				Err(refund) => {
					// cancel `refund` and include it back into `adjusted_paid`.
					adjusted_paid.merge(refund).map_or_else(
						|(adjusted_paid, refund)| {
							defensive!(
								"`adjusted_paid` and `refund` are credits of the same asset.",
								(adjusted_paid.asset(), refund.asset(), who)
							);
							// drop `refund` and return `adjusted_paid` without it.
							(fee_asset_amount, adjusted_paid)
						},
						|fee_paid| (fee_paid.peek(), fee_paid),
					)
				},
			};

			// Handle the imbalance (fee and tip separately).
			let (tip, fee) = adjusted_paid.split(tip);
			OU::on_unbalanceds(Some(fee).into_iter().chain(Some(tip)));
			return Ok(fee_asset_amount);
		}

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
			// The error should not occur since swap was quoted before.
			Err((refund, _)) => {
				defensive!(
					"Refund swap should pass for the quoted amount",
					(refund.asset(), refund.peek(), refund_asset_amount, who)
				);
				// cancel `refund` and include it back into `adjusted_paid`.
				adjusted_paid.merge(refund).map_or_else(
					|(adjusted_paid, refund)| {
						defensive!(
							"`adjusted_paid` and `refund` are credits of the same asset.",
							(adjusted_paid.asset(), refund.asset(), who)
						);
						// drop `refund` and return `adjusted_paid` without it.
						(fee_asset_amount, adjusted_paid)
					},
					|fee_paid| (fee_asset_amount, fee_paid),
				)
			},
		};

		// Handle the imbalance (fee and tip separately).
		let (tip, fee) = adjusted_paid.split(tip);
		OU::on_unbalanceds(Some(fee).into_iter().chain(Some(tip)));
		return Ok(fee_asset_amount);
	}
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L1499-1513)
```rust
		pub fn get_reserves(
			asset1: T::AssetKind,
			asset2: T::AssetKind,
		) -> Result<(T::Balance, T::Balance), Error<T>> {
			let pool_account = T::PoolLocator::pool_address(&asset1, &asset2)
				.map_err(|_| Error::<T>::InvalidAssetPair)?;

			let balance1 = Self::get_balance(&pool_account, asset1);
			let balance2 = Self::get_balance(&pool_account, asset2);

			if balance1.is_zero() || balance2.is_zero() {
				Err(Error::<T>::PoolEmpty)?;
			}

			Ok((balance1, balance2))
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L1516-1521)
```rust
		/// Gets a quote for swapping an exact amount of `asset1` for `asset2`.
		///
		/// If `include_fee` is true, the quote will include the liquidity provider fee.
		/// If the pool does not exist or has no liquidity, `None` is returned.
		/// Note that the price may have changed by the time the transaction is executed.
		/// (Use `amount_out_min` to control slippage.)
```
