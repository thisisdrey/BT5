## Analog Found [1](#0-0) 

### Title
Self-manipulated pool reserves in `ChargeAssetTxPayment` let a signed account extract AMM liquidity via the fee-refund swap - (File: `substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs`)

### Summary
`pallet-asset-conversion-tx-payment`'s `SwapAssetAdapter` prices transaction-fee conversions with the *current* (spot) reserves of a `pallet-asset-conversion` pool, exactly like the Uniswap-V3 report's use of `slot0`. Unlike the Uniswap case (which needs an external flash loan), here the price-changing action and the price-consuming action are **both inside the same extrinsic**: the extension quotes/withdraws the fee in `prepare()` using pre-call reserves, the user's own `call` executes and can itself be a swap on the very same pool, and then `post_dispatch_details()` re-quotes and swaps the refund using the **post-call** reserves. An unprivileged signed account can therefore choose a call that shifts the pool reserves in its own favor between the two price samples and pocket the difference from the pool's liquidity providers.

### Finding Description
The fee lifecycle for `ChargeAssetTxPayment` is: `validate` → `prepare` (calls `OnChargeAssetTransaction::withdraw_fee`) → dispatch the user's `call` → `post_dispatch_details` (calls `OnChargeAssetTransaction::correct_and_deposit_fee`). [2](#0-1) [3](#0-2) 

`SwapAssetAdapter::withdraw_fee` prices the fee with `S::quote_price_tokens_for_exact_tokens` against the pool reserves *before* the call runs: [4](#0-3) 

`SwapAssetAdapter::correct_and_deposit_fee`, invoked *after* the call has executed, re-prices the refund with `S::quote_price_exact_tokens_for_tokens(A::get(), asset_id, refund_amount, true)` against whatever reserves exist at that later point, then performs an actual `swap_exact_tokens_for_tokens` at that quoted rate: [5](#0-4) 

Both quote functions read `pallet_asset_conversion::Pallet::get_reserves`, which is simply the live token balances of the pool account — the AMM's spot price, with no TWAP or manipulation-resistance: [6](#0-5) 

The pallet's own documentation for `QuotePrice` acknowledges the price is only valid "if no other swaps are made after the price is quoted and before the target swap" — but the transaction-payment extension itself guarantees exactly that: the dispatched `call` runs strictly between the pre-dispatch quote/withdraw and the post-dispatch quote/refund-swap: [1](#0-0) 

An attacker submits a single extrinsic with:
1. `asset_id` set to some asset X paired with the fee asset `A::get()` in a `pallet-asset-conversion` pool.
2. `call` = a `pallet_asset_conversion::swap_*` call (or any call with the same economic effect) that pushes the X/A pool reserves so that `reserve_out` (asset X) is large relative to `reserve_in` (asset A) at the moment `correct_and_deposit_fee` runs.
3. The unspent-weight refund (`refund_amount` in asset A, computed from the difference between the estimated and actual weight) is then converted to asset X using `quote_price_exact_tokens_for_tokens`/`swap_exact_tokens_for_tokens` at the manipulated ratio, yielding X in excess of what the reserves warranted before manipulation. The extra X is extracted from the pool's liquidity providers, not created by legitimate trading activity, since the attacker fully controls both the manipulating trade and the beneficiary of the mispriced refund swap within one atomic transaction.

No existing guard stops this: `withdraw_fee`/`can_withdraw_fee` only check that the *pre-call* quote is nonzero and withdrawable; `correct_and_deposit_fee` only checks that the refund is nonzero and depositable (`can_deposit`) — neither compares the pre- and post-call reserves nor bounds the price movement caused by the user's own call.

### Impact Explanation
This directly matches the "public underpriced work" / "theft ... unbacked mint or unlock" pivot: a normal signed user, with no special privilege, can repeatedly drain value from `pallet-asset-conversion` liquidity pools configured as fee-payment pools, by sandwiching the pallet's own fee-refund conversion with a self-authored swap in the same extrinsic. This is chain-level fund loss for LPs on any runtime that wires `SwapAssetAdapter` as `OnChargeAssetTransaction` (e.g. Asset Hub runtimes), executable purely through the public transaction-payment path.

### Likelihood Explanation
The attack requires only: (a) a chain configuring `pallet-asset-conversion-tx-payment` with `SwapAssetAdapter`, (b) an existing pool for the chosen fee asset, and (c) crafting a call whose dispatch weight is over-estimated relative to actual usage (to generate a nonzero refund) while itself moving the pool reserves — both entirely within the attacker's control and repeatable per block. No relayer, validator, governance, or leaked-key assumption is needed, matching the "no malicious peer/admin" constraint of the impact gate.

### Recommendation
Do not re-quote the refund against post-call reserves. Options: (1) snapshot/lock the price used for `withdraw_fee` and reuse the same rate (or a bounded band around it) for `correct_and_deposit_fee`; (2) disallow calls that touch the same pool used for fee payment within the same extrinsic, or charge/refund fees against pool state captured strictly before dispatch and frozen through post-dispatch; (3) apply a maximum allowed price deviation between the pre- and post-call quotes, aborting the refund swap (keeping the excess in the fee pot) if exceeded, analogous to using a TWAP/oracle instead of spot state as recommended in the source report.

### Proof of Concept
1. Deploy/target a runtime with `pallet-asset-conversion` and `pallet-asset-conversion-tx-payment` (`SwapAssetAdapter`) enabled, with a shallow X/native pool.
2. Attacker account holds asset X and sets `ChargeAssetTxPayment { asset_id: Some(X), .. }`.
3. `call` = `AssetConversion::swap_tokens_for_exact_tokens` (or similar) trading native `A::get()` into X on the same pool, sized to shift `reserve_in(A)`/`reserve_out(X)` sharply, while leaving enough over-estimated weight so `post_info` produces a nonzero `unspent_weight`.
4. In `prepare`, `withdraw_fee` quotes/withdraws X at the pre-manipulation rate.
5. The call executes, moving the pool reserves as described.
6. In `post_dispatch_details` → `correct_and_deposit_fee`, `quote_price_exact_tokens_for_tokens(A::get(), X, refund_amount, true)` is computed against the now-skewed reserves (see [7](#0-6) ) and immediately swapped, crediting the attacker more X than the pre-manipulation reserves would have allowed for the same `refund_amount`.
7. Repeating across blocks lets the attacker continuously skim value from the pool's liquidity providers.

### Citations

**File:** substrate/frame/asset-conversion/src/swap.rs (L116-120)
```rust
/// Trait providing methods to quote swap prices between asset classes.
///
/// The quoted price is only guaranteed if no other swaps are made after the price is quoted and
/// before the target swap (e.g., the swap is made immediately within the same transaction).
pub trait QuotePrice {
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

**File:** substrate/frame/asset-conversion/src/lib.rs (L1499-1514)
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
		}
```
