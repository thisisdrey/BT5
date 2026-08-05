The code review confirms the claim's technical description is accurate. `ChargeAssetTxPayment` in `substrate/frame/transaction-payment/asset-conversion-tx-payment/src/lib.rs` has only `tip` and `asset_id` fields with no slippage/max-fee parameter, and `SwapAssetAdapter::withdraw_fee`/`correct_and_deposit_fee` in `payment.rs` quote the AMM price via `quote_price_tokens_for_exact_tokens`/`quote_price_exact_tokens_for_tokens` and immediately consume that exact quote, with no caller-supplied bound.

Audit Report

## Title
`ChargeAssetTxPayment` withdraws non-native fee assets using an unbounded on-chain AMM quote, exposing every asset-fee-paying user to sandwich attacks - ([File: substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs])

## Summary
`SwapAssetAdapter::withdraw_fee` quotes the amount of a user's chosen fee asset needed via `S::quote_price_tokens_for_exact_tokens` and immediately withdraws and swaps that exact amount, with no caller-supplied maximum. `ChargeAssetTxPayment` exposes only `tip` and `asset_id` fields and no slippage bound, unlike the pallet's own swap extrinsics which require `amount_out_min`/`amount_in_max`.

## Finding Description
`withdraw_fee` in `substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs` computes `asset_fee` from live AMM reserves via `S::quote_price_tokens_for_exact_tokens(asset_id, A::get(), fee, true)` [1](#0-0) , withdraws exactly that amount from the user, and swaps it for the native `fee` [2](#0-1) . `correct_and_deposit_fee` performs the analogous quote-then-swap for the refund leg [3](#0-2) .

`ChargeAssetTxPayment` itself carries only `tip` and `asset_id` — no field lets the fee-payer bound the maximum `asset_id` amount they are willing to spend [4](#0-3) , and `withdraw_fee` in `lib.rs` passes through to `OnChargeAssetTransaction::withdraw_fee` with no such bound available [5](#0-4) . This runs inside `prepare` for every signed extrinsic that selects a non-native `asset_id`, a fully public and unprivileged path [6](#0-5) .

By contrast, the pallet's own `swap_exact_tokens_for_tokens`/`swap_tokens_for_exact_tokens` extrinsics in `substrate/frame/asset-conversion/src/lib.rs` require and enforce `amount_out_min`/`amount_in_max` guards before allowing a swap to complete, exactly the protection missing from the fee-conversion path. The `defensive!` assertions in `payment.rs` only catch inconsistency between the quote and swap calls made back-to-back in the same function call — they provide no protection against the pool price having been manipulated between when the extrinsic is constructed/broadcast and when `prepare` executes on-chain.

An attacker can sandwich the victim's fee-conversion swap: submit a transaction immediately before the victim's to skew the `asset_id`/native pool ratio, let the victim's `withdraw_fee` quote and pay at the manipulated price, then submit a reversing transaction immediately after to capture the spread. This requires no privileged role, collusion, or key compromise — only the ordinary ability to submit and order transactions within a block.

## Impact Explanation
This falls into the "public underpriced work" / unbacked value transfer impact class: any account paying fees in a non-native asset via `pallet-asset-conversion-tx-payment` (wired via `SwapAssetAdapter` in the reference node runtime and the Penpal parachain) can have its effective fee inflated with no cap, extracting value from the victim via a completely public, unprivileged mechanism.

## Likelihood Explanation
Medium-to-High. The attack is a standard sandwich pattern requiring only ordinary transaction submission with favorable ordering/priority in the same block — no validator, collator, or relayer collusion is needed, and no governance action is required. Feasibility scales with pool liquidity depth versus the fee amount being converted.

## Recommendation
Add a caller-supplied maximum acceptable `asset_id` amount (analogous to `amount_in_max`) to `ChargeAssetTxPayment`/`OnChargeAssetTransaction::withdraw_fee`, and fail validation if the quoted `asset_fee` exceeds it. Apply an analogous minimum-acceptable-refund bound in `correct_and_deposit_fee`. Consider a TWAP or oracle-based price source instead of raw spot AMM reserves for fee conversion.

## Proof of Concept
1. Attacker observes a pending transaction using `ChargeAssetTxPayment { asset_id: Some(X), .. }` against pool `(X, Native)`.
2. Attacker submits a front-running swap that skews the `X`/`Native` ratio so native tokens become expensive in terms of `X`.
3. Victim's `withdraw_fee` quotes and swaps at the manipulated price via `S::quote_price_tokens_for_exact_tokens` / `S::swap_tokens_for_exact_tokens`, paying an inflated amount of `X` with no cap available to the victim.
4. Attacker submits a reversing swap, capturing the spread as profit.

### Citations

**File:** substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs (L142-146)
```rust
		// Quote the amount of the `asset_id` needed to pay the fee in the asset `A`.
		let asset_fee =
			S::quote_price_tokens_for_exact_tokens(asset_id.clone(), A::get(), fee, true)
				.filter(|asset_fee| !asset_fee.is_zero())
				.ok_or(InvalidTransaction::Payment)?;
```

**File:** substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs (L148-176)
```rust
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

**File:** substrate/frame/transaction-payment/asset-conversion-tx-payment/src/lib.rs (L176-182)
```rust
#[derive(Encode, Decode, DecodeWithMemTracking, Clone, Eq, PartialEq, TypeInfo)]
#[scale_info(skip_type_params(T))]
pub struct ChargeAssetTxPayment<T: Config> {
	#[codec(compact)]
	tip: BalanceOf<T>,
	asset_id: Option<T::AssetId>,
}
```

**File:** substrate/frame/transaction-payment/asset-conversion-tx-payment/src/lib.rs (L193-219)
```rust
	/// Fee withdrawal logic that dispatches to either [`Config::OnChargeAssetTransaction`] or
	/// [`pallet_transaction_payment::Config::OnChargeTransaction`].
	fn withdraw_fee(
		&self,
		who: &T::AccountId,
		call: &T::RuntimeCall,
		info: &DispatchInfoOf<T::RuntimeCall>,
		fee: BalanceOf<T>,
	) -> Result<(BalanceOf<T>, InitialPayment<T>), TransactionValidityError> {
		debug_assert!(self.tip <= fee, "tip should be included in the computed fee");
		if fee.is_zero() {
			Ok((fee, InitialPayment::Nothing))
		} else if let Some(asset_id) = &self.asset_id {
			T::OnChargeAssetTransaction::withdraw_fee(
				who,
				call,
				info,
				asset_id.clone(),
				fee,
				self.tip,
			)
			.map(|payment| (fee, InitialPayment::Asset((asset_id.clone(), payment))))
		} else {
			T::OnChargeTransaction::withdraw_fee(who, call, info, fee, self.tip)
				.map(|payment| (fee, InitialPayment::Native(payment)))
		}
	}
```

**File:** substrate/frame/transaction-payment/asset-conversion-tx-payment/src/lib.rs (L327-343)
```rust
	fn prepare(
		self,
		val: Self::Val,
		_origin: &<T::RuntimeCall as Dispatchable>::RuntimeOrigin,
		call: &T::RuntimeCall,
		info: &DispatchInfoOf<T::RuntimeCall>,
		_len: usize,
	) -> Result<Self::Pre, TransactionValidityError> {
		match val {
			Val::Charge { tip, who, fee } => {
				// Mutating call of `withdraw_fee` to actually charge for the transaction.
				let (_fee, initial_payment) = self.withdraw_fee(&who, call, info, fee)?;
				Ok(Pre::Charge { tip, who, initial_payment, weight: self.weight(call) })
			},
			Val::NoCharge => Ok(Pre::NoCharge { refund: self.weight(call) }),
		}
	}
```
