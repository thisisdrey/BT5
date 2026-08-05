### Title
Transaction fees paid in non-native assets are priced from manipulable single-block spot pool reserves, allowing fee-value evasion via self-sandwiching - ([File: substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs])

### Summary
`pallet-asset-conversion-tx-payment`'s `SwapAssetAdapter::withdraw_fee` and `can_withdraw_fee` price a transaction's native-asset fee in an alternate asset by calling `S::quote_price_tokens_for_exact_tokens`, which resolves to `pallet-asset-conversion`'s `quote_price_tokens_for_exact_tokens` → `get_reserves` → the pool account's *current, instantaneous* balances [1](#0-0) [2](#0-1) [3](#0-2) . This is the same broken invariant as the external report: an on-chain value (LP/AMM price) is computed directly from spot reserves that a single account can move within one block, and that manipulated value is then used to settle real value (there: LP collateral price; here: the amount of asset debited to pay a chain fee).

### Finding Description
The external report's core primitive is: "TVL/price = f(current reserves)", and reserves can be inflated/skewed by dumping tokens into the pool in the same block, with no TWAP or manipulation resistance, so the derived price is wrong for that block.

The Polkadot SDK analog is structurally identical in `pallet-asset-conversion`:
- `get_reserves` reads live pool-account balances with no time-weighting, no minimum observation window, and no manipulation guard [3](#0-2) .
- `quote_price_tokens_for_exact_tokens`/`quote_price_exact_tokens_for_tokens` derive an output/input amount purely from those spot balances via `get_amount_in`/`get_amount_out`/`quote` [2](#0-1) [4](#0-3) .
- `SwapAssetAdapter::withdraw_fee`, used by the public `ChargeAssetTxPayment` transaction extension to let *any* signed account pay dispatch fees in a non-native asset, calls this exact spot quote to decide how much of the user's asset to withdraw for a fixed native fee, then performs an exact-output swap against the same pool at whatever price the quote produced [5](#0-4) .

Because a signed account fully controls the ordering of its own extrinsics within the transaction pool up to the point of block authoring (multiple transactions from the same sender, submitted with sequential nonces, are executed in nonce order in the same block), an attacker can:
1. Submit tx #1: a large `swap_exact_tokens_for_tokens`/`swap_tokens_for_exact_tokens` against the `asset_id`/native pool that shifts the spot reserve ratio so that `asset_id` looks artificially "expensive" relative to native (i.e., fewer `asset_id` tokens are quoted per unit of native fee).
2. Submit tx #2 in the same block: any call paid for with `ChargeAssetTxPayment` in `asset_id`. `withdraw_fee` quotes the fee using the reserves as skewed by tx #1, underpaying the true (pre-manipulation) value of `asset_id` relative to the native fee actually consumed by the runtime.
3. Submit tx #3 (optional) reversing the swap to restore the pool and realize/retain the extracted value, or simply let normal trading re-equilibrate the pool afterward.

This requires no malicious validator, collator, relayer, or admin — only an ordinary signed account able to submit ordinary swap extrinsics and a fee-paying extrinsic, which is exactly the "unprivileged public entrypoint" path the task calls for. No existing guard in `withdraw_fee`/`can_withdraw_fee` checks for reserve deviation, uses a TWAP, or bounds the quoted price against a reference/oracle price; the only protections present (`filter(|asset_fee| !asset_fee.is_zero())`, the exact-swap `change.peek().is_zero()` check) validate arithmetic correctness of the swap, not price integrity [6](#0-5) .

### Impact Explanation
The corrupted value is the quoted `asset_fee` amount withdrawn in `withdraw_fee` (and mirrored in `can_withdraw_fee`) [1](#0-0) . Because this amount directly determines how much of the user's non-native asset is debited to cover a fixed native-fee obligation, an attacker who transiently skews the pool can systematically underpay the intended fee value across many transactions, extracting value from the fee-recipient side (block author/treasury, depending on `OU`) or, symmetrically, forcing other unrelated users who pay fees via the same pool at the same block to over/under-pay relative to fair value. This is a value-conservation violation of the exact kind called out in the "Polkadot SDK Pivots": settlement (fee payment) does not correspond to the correct amount/beneficiary value.

### Likelihood Explanation
Likelihood is high for AMM pools with shallow liquidity, since `pallet-asset-conversion` pools are permissionlessly creatable and permissionlessly tradeable, and there is no minimum-liquidity or price-deviation safeguard tying the tx-payment quote to a longer-window price. The attack is executable entirely by an unprivileged signed account within a single block using only public extrinsics (`swap_*` + a `ChargeAssetTxPayment`-extended call), with no dependency on validator/collator behavior, front-running by third parties, or governance.

### Recommendation
Do not price non-native transaction fees from raw instantaneous AMM reserves. Either (a) bound the `SwapAssetAdapter` quote against a manipulation-resistant reference (e.g., a time-weighted average price or a governance-configured price band) before using it to withdraw fees, or (b) require the quote used in `withdraw_fee`/`can_withdraw_fee` to be validated against pool state from a prior block (a checkpointed reserve snapshot) rather than the reserves visible in the block currently being authored, closing the self-sandwich window entirely.

### Proof of Concept
1. Create an `asset_id`↔native pool with shallow liquidity via `pallet-asset-conversion::create_pool`/`add_liquidity`.
2. From a single signed account, submit (same block, sequential nonces):
   - tx A: `swap_exact_tokens_for_tokens` dumping a large amount of native (or `asset_id`) into the pool to skew reserves.
   - tx B: any call wrapped with `ChargeAssetTxPayment::<Runtime>::from(tip, Some(asset_id))`, whose `validate_and_prepare` invokes `SwapAssetAdapter::withdraw_fee` → `quote_price_tokens_for_exact_tokens` against the reserves as skewed by tx A [1](#0-0) .
   - tx C (optional): reverse swap to restore the pool.
3. Compare `Assets::balance(asset_id, caller)` debited in tx B to the amount that would have been quoted absent tx A (as demonstrated by the existing test `transaction_payment_in_asset_possible`, which shows the exact quote-driven debit path and value `assert_eq!(input_quote, Some(201))` under un-skewed reserves) [7](#0-6) ; skewing reserves via tx A changes this quoted value materially for the same native fee, confirming the underpricing/overpricing primitive.

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

**File:** substrate/frame/asset-conversion/src/lib.rs (L1523-1546)
```rust
		pub fn quote_price_exact_tokens_for_tokens(
			asset1: T::AssetKind,
			asset2: T::AssetKind,
			amount: T::Balance,
			include_fee: bool,
		) -> Option<T::Balance> {
			// Swaps reject zero amounts, match that behavior.
			if amount.is_zero() {
				return None;
			}

			let pool_account = T::PoolLocator::pool_address(&asset1, &asset2).ok()?;

			let (balance1, balance2) = Self::get_reserves(asset1.clone(), asset2.clone()).ok()?;

			if balance1.is_zero() {
				return None;
			}

			let amount_out = if include_fee {
				let fee = Self::pool_fee_for(&asset1, &asset2).ok()?;
				Self::get_amount_out(fee, &amount, &balance1, &balance2).ok()?
			} else {
				Self::quote(&amount, &balance1, &balance2).ok()?
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L1571-1603)
```rust
		pub fn quote_price_tokens_for_exact_tokens(
			asset1: T::AssetKind,
			asset2: T::AssetKind,
			amount: T::Balance,
			include_fee: bool,
		) -> Option<T::Balance> {
			// Swaps reject zero amounts, match that behavior.
			if amount.is_zero() {
				return None;
			}
			let pool_account = T::PoolLocator::pool_address(&asset1, &asset2).ok()?;

			let (balance1, balance2) = Self::get_reserves(asset1.clone(), asset2.clone()).ok()?;

			if balance1.is_zero() {
				return None;
			}

			// Swap withdrawals from pools use `keep_alive=true` (Preserve). Use the same
			// preservation level to determine the actual withdrawable amount.
			let max_output =
				T::Assets::reducible_balance(asset2.clone(), &pool_account, Preserve, Polite);
			if amount > max_output {
				return None;
			}

			if include_fee {
				let fee = Self::pool_fee_for(&asset1, &asset2).ok()?;
				Self::get_amount_in(fee, &amount, &balance1, &balance2).ok()
			} else {
				Self::quote(&amount, &balance2, &balance1).ok()
			}
		}
```

**File:** substrate/frame/transaction-payment/asset-conversion-tx-payment/src/tests.rs (L244-256)
```rust
			setup_lp(asset_id, balance_factor);

			let fee_in_native = base_weight + tx_weight + len as u64;
			let input_quote = AssetConversion::quote_price_tokens_for_exact_tokens(
				NativeOrWithId::WithId(asset_id),
				NativeOrWithId::Native,
				fee_in_native,
				true,
			);
			assert_eq!(input_quote, Some(201));

			let fee_in_asset = input_quote.unwrap();
			assert_eq!(Assets::balance(asset_id, caller), balance);
```
