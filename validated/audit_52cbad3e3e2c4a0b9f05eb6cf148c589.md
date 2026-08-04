## Analysis

The Tokemak bug's core broken invariant is: **a redemption amount is computed from a stale valuation, then settled through an unprotected AMM swap whose execution price the attacker controls within the same transaction** — letting the attacker capture the valuation gap instead of the protocol/LPs.

The closest verifiable local analog in this repository is in `pallet-asset-conversion-tx-payment`, where transaction fees paid in a non-native asset are withdrawn using one AMM quote and later refunded using a second AMM quote taken *after* the user's own dispatched call has executed — with no anchor back to the original price.

### Title
Self-referential AMM price used for `ChargeAssetTxPayment` fee refund lets the payer drain `pallet-asset-conversion` pool liquidity by manipulating reserves inside their own dispatched call - (File: `substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs`)

### Summary
`ChargeAssetTxPayment`'s `SwapAssetAdapter` converts a user's chosen `asset_id` into the native fee asset at `prepare()` time using a spot AMM quote (`quote_price_tokens_for_exact_tokens`), then, after the wrapped call has been fully dispatched, computes a refund by re-quoting and swapping *back* into `asset_id` using `quote_price_exact_tokens_for_tokens` / `swap_exact_tokens_for_tokens` against whatever the pool's reserves are **at that later moment**. The dispatched call is entirely attacker-controlled and can itself be (or contain) an `AssetConversion` operation on the exact same pool pair, shifting reserves before the refund swap executes. The `amount_out_min` used to "protect" that refund swap is derived from the very same post-manipulation state, so it enforces nothing against the manipulation — it only protects against a third party racing between quote and execution, which cannot happen inside one atomic extrinsic.

### Finding Description
`withdraw_fee` (prepare/pre-dispatch) locks in a fee-asset amount using the pool state visible before dispatch: [1](#0-0) 

The wrapped call is then dispatched with no restriction preventing it from touching the same `AssetConversion` pool (e.g. `swap_exact_tokens_for_tokens`, `add_liquidity`, `remove_liquidity`) used to price the fee, as seen in `prepare`/`post_dispatch_details`: [2](#0-1) 

After dispatch, `correct_and_deposit_fee` computes the refund and re-quotes/re-swaps against the *current* (now attacker-shifted) reserves: [3](#0-2) 

The `amount_out_min` passed to `swap_exact_tokens_for_tokens` is `refund_asset_amount`, which was itself just derived from `quote_price_exact_tokens_for_tokens` against the same manipulated reserves — it is not compared against the original pre-dispatch price used in `withdraw_fee`. Nothing in this path re-derives or bounds the refund relative to the price that was actually used to charge the fee.

This exactly mirrors the reported bug class: a value computed from one price snapshot (`withdraw_fee`'s quote) is settled through a second, unguarded AMM interaction (`correct_and_deposit_fee`'s swap) whose price can be freely set by the same party who triggered the whole flow, because the intervening dispatched call is fully under their control and hits the same pool.

### Impact Explanation
An attacker who pays fees in `asset_id` can, in a single extrinsic:
1. Submit a call with an over-estimated weight (or one with variable/refundable weight) that also performs a swap/liquidity operation on the same `AssetConversion` pool pair used for fee payment, shifting reserves so that native→`asset_id` becomes maximally favorable.
2. Rely on `correct_and_deposit_fee`'s post-dispatch refund swap, which re-quotes and executes entirely against this attacker-shifted pool state.

The result is that the refund the attacker receives in `asset_id` is priced off a state they just created, rather than the state that determined how much `asset_id` they were actually charged — extracting value from the pool's liquidity providers rather than paying/receiving a fair, price-consistent refund. This is a public, unprivileged-user-reachable path with direct value transfer from the AMM pool (shared with other users) to the transaction's fee payer, i.e. theft/unbacked extraction of pool value, without needing a malicious peer, validator, or governance actor.

### Likelihood Explanation
Any account can construct such a transaction: choosing `asset_id` for `ChargeAssetTxPayment`, and having the dispatched call itself interact with the very same pool (a completely ordinary, permissionless action — swapping or adding/removing liquidity). No special privileges, collusion, or off-chain infrastructure are required; the manipulation and the refund both occur deterministically inside a single, self-contained extrinsic that the attacker fully controls, so the attack is reliably reproducible on any chain enabling `pallet-asset-conversion-tx-payment` with `SwapAssetAdapter`.

### Recommendation
Do not let `correct_and_deposit_fee` derive the refund's exchange rate purely from the pool state observed after the wrapped call executes. Either:
- Persist the effective price (or the reserves) captured at `withdraw_fee` time and bound the refund swap's execution price to be no more favorable than that original price (a true price-invariant `amount_out_min`, not one re-derived from post-dispatch state), or
- Disallow/flag the case where the dispatched call mutates the same pool used for fee conversion within the same extrinsic, or
- Use a time-weighted/guarded price source instead of the instantaneous pool spot price for both legs of the fee conversion.

### Proof of Concept
1. Create an `AssetConversion` pool for `(Native, asset_id)` with liquidity `(N, A)`.
2. Attacker submits an extrinsic with `ChargeAssetTxPayment::from(tip, Some(asset_id))`:
   - `prepare()` quotes and withdraws `asset_fee` of `asset_id` for the declared `fee` in native, per `withdraw_fee` (`substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs:142-175`).
   - The dispatched call itself invokes `pallet_asset_conversion::swap_exact_tokens_for_tokens` on `(Native, asset_id)`, selling a large amount of `asset_id` into the pool, inflating `A` and depleting `N`, and is constructed to consume much less weight than declared (or to be a call whose weight is heavily refunded).
3. `post_dispatch_details` computes a large `refund_amount` (native) from the weight difference, then `correct_and_deposit_fee` re-quotes `quote_price_exact_tokens_for_tokens(Native, asset_id, refund_amount, true)` against the now `asset_id`-heavy pool and swaps at that inflated rate (`payment.rs:259-297`), returning far more `asset_id` to the attacker than the original `asset_fee` conversion rate would justify.
4. Net effect: the attacker recovers more `asset_id` value than is consistent with the price at which they were charged, at the expense of the pool's other liquidity providers, purely by sequencing their own manipulation call between the two AMM quotes used by the fee-payment extension.

### Citations

**File:** substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs (L142-175)
```rust
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
```

**File:** substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs (L259-297)
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
