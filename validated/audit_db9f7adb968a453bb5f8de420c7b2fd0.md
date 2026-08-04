This maps well onto a genuine local analog: `pallet-asset-conversion-tx-payment`'s `SwapAssetAdapter` reads spot price from `pallet-asset-conversion`'s AMM pool both before *and* after the wrapped call executes — and the wrapped call is fully attacker-controlled within the very same atomic transaction, which is exactly the "manipulate the AMM within one transaction, then get paid at the manipulated rate" primitive from the Sandclock report.

### Title
Fee-refund settlement in `pallet-asset-conversion-tx-payment` prices against post-call AMM spot reserves, letting the wrapped call manipulate its own fee-asset payout - (File: `substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs`)

### Summary
`ChargeAssetTxPayment`'s `prepare` withdraws the fee by swapping the user's chosen `asset_id` into the native asset at the AMM's *current* spot price, then dispatches the wrapped `call`, then in `post_dispatch_details`/`correct_and_deposit_fee` swaps any refund back from native asset into `asset_id` using the AMM's spot price *after* the call ran [1](#0-0) . Because the wrapped `call` is dispatched by the very same signed origin and can itself be (or contain, via `pallet-utility` batching) a swap/`add_liquidity`/`remove_liquidity` against the exact pool used to price the refund, the origin can shift the pool's reserves between the pre-dispatch pricing and the post-dispatch refund pricing, all inside one atomic transaction — with no staleness or deviation check protecting the refund leg.

### Finding Description
`pallet_asset_conversion::QuotePrice` explicitly documents that its quoted price "is only guaranteed if no other swaps are made after the price is quoted and before the target swap (e.g., the swap is made immediately within the same transaction)" [2](#0-1) . `SwapAssetAdapter::correct_and_deposit_fee` violates this assumption by design: it re-quotes and re-swaps using `S::quote_price_exact_tokens_for_tokens(A::get(), asset_id, refund_amount, true)` against the pool's live reserves at post-dispatch time [3](#0-2) , and this happens strictly after the wrapped `call` has already been dispatched and committed [4](#0-3) .

The `call` in between is entirely attacker-controlled content of the same extrinsic (it can be `pallet_asset_conversion::swap_exact_tokens_for_tokens`, `add_liquidity`, `remove_liquidity` on that same pool, or a `pallet-utility::batch_all` containing such a swap alongside other logic). This gives the transaction signer a single-transaction window to:
1. Pay the fee up-front by swapping `asset_id` → native at the pre-manipulation price (unavoidable, normal cost).
2. Inside `call`, dump/pull liquidity or swap heavily against the `asset_id`/native pool to skew its reserve ratio in the attacker's favor (classic flash-loan-style spot manipulation, minus even needing an external flash loan since it's the same tx).
3. Let `correct_and_deposit_fee` swap the leftover refund (`fee_paid - corrected_fee`, driven by weight-refund accounting which the attacker also influences via the batch's actual vs estimated weight) back into `asset_id` at the now-skewed, attacker-favorable price, extracting more `asset_id` from the pool's liquidity providers than is economically justified.

No oracle, TWAP, or "price hasn't moved" guard exists on this refund path — `defensive!` macros in the surrounding code only guard against logic bugs that "should never happen," not economic manipulation of reserves by the dispatched call itself [5](#0-4) . This is structurally the same bug class as `NonUSTStrategy`: a public entry point that both quotes and reprices against a spot-priced pool the caller can move within the atomic execution window that separates "price captured" from "value settled."

### Impact Explanation
An attacker who pays transaction fees via a thin/low-liquidity `asset_id` pool can extract pool liquidity-provider funds by manipulating the pool's reserves via their own call body before the post-dispatch refund swap prices against the manipulated reserves. This is fund loss/drain from LPs of the affected asset-conversion pool, triggerable by any ordinary unprivileged signed account choosing `asset_id` for fee payment — no validator, relayer, governance, or privileged role is required.

### Likelihood Explanation
Exploitability depends on: (a) a low-liquidity pool for the fee-paying `asset_id`, so a modest swap materially shifts price, and (b) enough refund magnitude (driven by weight over-estimation vs actual weight consumed, which callers substantially control by crafting the call/batch). Both conditions are within an unprivileged attacker's control, making this practically exploitable on any runtime that enables `pallet-asset-conversion-tx-payment` with `SwapAssetAdapter` for a pool the attacker can also trade against in the same extrinsic (e.g., via `pallet-utility::batch_all`).

### Recommendation
- Do not re-derive the refund conversion rate from live pool reserves captured after the wrapped `call` executes. Cache/lock the price (or bound the allowable deviation) at `withdraw_fee` time and reuse it, or refund strictly in the native asset and let the user perform any conversion themselves via a separate, later transaction.
- Alternatively, disallow calls that trade against the same asset pair being used for fee payment within the same extrinsic, or apply slippage/deviation checks comparing pre- and post-dispatch spot prices before executing the refund swap.

### Proof of Concept
1. Attacker sets up (or finds) a thin `asset_id`/native `AssetConversion` pool.
2. Submit an extrinsic with `ChargeAssetTxPayment { asset_id: Some(asset_id), .. }` wrapping a `pallet_utility::batch_all` call that: (a) performs a large `AssetConversion::swap_exact_tokens_for_tokens` or `remove_liquidity` against the exact `asset_id`/native pool to skew its reserves, and (b) is crafted so the declared vs. actual dispatch weight produces a non-trivial `refund_amount`.
3. `prepare` withdraws/swaps the fee at pre-manipulation price [6](#0-5) .
4. The batched call executes and skews reserves.
5. `post_dispatch_details` → `correct_and_deposit_fee` quotes and swaps the refund back into `asset_id` at the now-skewed reserves [7](#0-6) , crediting the attacker more `asset_id` than the pre-manipulation exchange rate would allow, at the expense of the pool's liquidity providers.

### Citations

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

**File:** substrate/frame/transaction-payment/asset-conversion-tx-payment/src/lib.rs (L345-360)
```rust
	fn post_dispatch_details(
		pre: Self::Pre,
		info: &DispatchInfoOf<T::RuntimeCall>,
		post_info: &PostDispatchInfoOf<T::RuntimeCall>,
		len: usize,
		_result: &DispatchResult,
	) -> Result<Weight, TransactionValidityError> {
		let (tip, who, initial_payment, extension_weight) = match pre {
			Pre::Charge { tip, who, initial_payment, weight } => {
				(tip, who, initial_payment, weight)
			},
			Pre::NoCharge { refund } => {
				// No-op: Refund everything
				return Ok(refund);
			},
		};
```

**File:** substrate/frame/asset-conversion/src/swap.rs (L116-120)
```rust
/// Trait providing methods to quote swap prices between asset classes.
///
/// The quoted price is only guaranteed if no other swaps are made after the price is quoted and
/// before the target swap (e.g., the swap is made immediately within the same transaction).
pub trait QuotePrice {
```

**File:** substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs (L119-157)
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
```

**File:** substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs (L259-289)
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
```

**File:** substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs (L298-317)
```rust
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
