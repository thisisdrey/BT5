## Analysis

The external report's core broken invariant: a protocol treats an AMM pool's **current/spot reserve ratio** as a trusted price oracle for computing a settlement amount, with no TWAP, no bound tied to a pre-manipulation reference price, and no check that the price wasn't just moved by the same caller.

The direct on-chain analog is `pallet-asset-conversion-tx-payment`'s fee-refund logic, `SwapAssetAdapter::correct_and_deposit_fee` in `substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs`.

### Title
Fee-refund logic in `SwapAssetAdapter::correct_and_deposit_fee` uses post-dispatch AMM spot price, letting a user manipulate their own asset-fee refund - (File: `substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs`)

### Summary
`ChargeAssetTxPayment`'s fee is withdrawn *before* the wrapped call dispatches (in `prepare`, using the pool state at that moment), but the refund is computed and executed *after* the call has run, using `S::quote_price_exact_tokens_for_tokens` / `S::swap_exact_tokens_for_tokens` against whatever pool state exists at `post_dispatch_details` time. Because the wrapped call itself can perform an `pallet_asset_conversion` swap on that very pool, the caller fully controls the "spot price" that determines how much of their fee-asset they get refunded, exactly the spot-price-as-oracle flaw described in the external report.

### Finding Description
The extension's lifecycle is:
1. `validate`/`prepare` → `SwapAssetAdapter::withdraw_fee` quotes `S::quote_price_tokens_for_exact_tokens(asset_id, A, fee, true)` at the **pre-dispatch** pool ratio and withdraws that many units of `asset_id` from the user, swapping them into the fee asset `A`. [1](#0-0) 
2. The wrapped `call` then dispatches. Nothing prevents this call from itself invoking `pallet_asset_conversion::swap_exact_tokens_for_tokens` (or `swap_tokens_for_exact_tokens`) on the identical `asset_id <-> A` pool, arbitrarily skewing the reserve ratio.
3. `post_dispatch_details` → `correct_and_deposit_fee` computes `refund_amount` in `A`, then quotes the refund back into `asset_id` via `S::quote_price_exact_tokens_for_tokens(A, asset_id, refund_amount, true)` and executes `S::swap_exact_tokens_for_tokens(vec![A, asset_id], refund, Some(refund_asset_amount))` — all using the **post-dispatch (attacker-controlled) pool state**. [2](#0-1) 

`pallet_asset_conversion`'s own quoting functions explicitly document that they reflect only the current pool balances (spot price) with the caveat that "the price may have changed by the time the transaction is executed" — a caveat meant for cross-transaction slippage, not for a price the *same* transaction's own call just moved. [3](#0-2) [4](#0-3) 

There is no re-validation that the pool state used for the refund matches the state used for the initial withdrawal, no TWAP, and no bound tying the refund quote to the price actually paid at withdrawal time.

### Impact Explanation
A user paying fees in a non-native asset can embed an `AssetConversion` swap in the very call protected by `ChargeAssetTxPayment` (directly, or wrapped via `pallet-utility::batch`/`batch_all`) to shift the fee-asset/native pool's reserves before the automatic refund swap executes. The refund swap then executes at the manipulated ratio, letting the attacker extract additional `asset_id` from the pool — funded by the pool's liquidity providers — beyond what the fee actually cost at withdrawal time. This is value theft from AMM LPs routed through a protocol-privileged, unrestricted swap call, directly matching "theft or unbacked mint" and "public underpriced work" impact classes.

### Likelihood Explanation
Medium-to-high: this requires only an ordinary signed account able to (a) hold both the fee asset and enough liquidity/capital to move the pool, and (b) construct a single extrinsic (call or `utility.batch`) that both swaps the pool and triggers a weight/`Pays::No` overestimation so `corrected_fee < fee` (e.g., dispatching a call whose actual weight is far below its declared `DispatchInfo`, or one that returns `Pays::No`). No malicious peer, validator, collator, or governance actor is needed — everything happens within one atomic transaction from an unprivileged account.

### Recommendation
- Snapshot the pool exchange rate (or lock/quote it) once, at `prepare` time, and reuse that same quote for both the withdrawal and the refund, rather than re-querying spot price after the call dispatches.
- Alternatively, disallow/detect nested calls to the same pool used for fee-asset conversion within the extension-protected call, or clamp the refund quote to be no more favorable than the rate at withdrawal time.
- More generally, avoid using un-time-weighted `pallet_asset_conversion::get_reserves`-derived spot quotes for any settlement that spans a user-controlled call boundary.

### Proof of Concept
1. Attacker holds asset `X` and pays tx fees using `ChargeAssetTxPayment { asset_id: Some(X) }`.
2. Attacker submits a `pallet_utility::batch_all` call as the dispatched call, containing:
   a. A large `pallet_asset_conversion::swap_exact_tokens_for_tokens` trade dumping `X` into the `X<->Native` pool, sharply moving the reserve ratio so that `Native` becomes "expensive" in terms of `X` (i.e., 1 unit of `Native` now quotes for far more `X` than before).
   b. A cheap/`Pays::No` inner call so that `post_info` weight/fee is much smaller than the originally withdrawn `fee`.
3. `withdraw_fee` (pre-dispatch) already converted a modest amount of `X` into `Native` at the pre-manipulation ratio.
4. `correct_and_deposit_fee` (post-dispatch) computes a large `refund_amount` in `Native` (because step 2b made the actual fee tiny) and quotes/executes the refund swap `Native -> X` at the ratio manipulated in step 2a, yielding an inflated amount of `X` back to the attacker — more `X` than the fee-asset value actually consumed, drained from the pool's LPs.
5. Repeat across pool depth to extract value, bounded only by the attacker's capital to move the pool and the swap fee overhead, which the induced refund mispricing can exceed. [5](#0-4)

### Citations

**File:** substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs (L142-176)
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
	}
```

**File:** substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs (L259-317)
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
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L1516-1522)
```rust
		/// Gets a quote for swapping an exact amount of `asset1` for `asset2`.
		///
		/// If `include_fee` is true, the quote will include the liquidity provider fee.
		/// If the pool does not exist or has no liquidity, `None` is returned.
		/// Note that the price may have changed by the time the transaction is executed.
		/// (Use `amount_out_min` to control slippage.)
		/// Returns `Some(quoted_amount)` on success.
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L1564-1570)
```rust
		/// Gets a quote for swapping `amount` of `asset1` for an exact amount of `asset2`.
		///
		/// If `include_fee` is true, the quote will include the liquidity provider fee.
		/// If the pool does not exist or has no liquidity, `None` is returned.
		/// Note that the price may have changed by the time the transaction is executed.
		/// (Use `amount_in_max` to control slippage.)
		/// Returns `Some(quoted_amount)` on success.
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
