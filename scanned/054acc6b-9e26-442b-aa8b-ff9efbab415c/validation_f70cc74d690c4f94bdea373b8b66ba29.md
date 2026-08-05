## Analysis

The GammaVault bug class — using an instantaneous/spot AMM price instead of a manipulation-resistant price (TWAP) for a value-critical calculation — has a direct analog in `pallet-asset-conversion-tx-payment`'s `SwapAssetAdapter`, which prices transaction fees paid in a non-native asset using the *current* spot reserves of `pallet-asset-conversion`, with no time-weighting or manipulation resistance.

### Title
Transaction fee payment via `SwapAssetAdapter` prices assets using manipulable spot AMM reserves, enabling fee underpayment and AMM pool value drain - (File: `substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs`)

### Summary
`SwapAssetAdapter::withdraw_fee` (used by `ChargeAssetTxPayment`) determines how much of a non-native asset a signer must pay for transaction fees by calling `S::quote_price_tokens_for_exact_tokens`, which is implemented by `pallet_asset_conversion::Pallet::quote_price_tokens_for_exact_tokens`. That function reads the pool's *live* token balances via `get_reserves` and computes the price with `get_amount_in`/`quote`, i.e. the instantaneous spot price of the constant-product pool — the exact same class of manipulable price source flagged in the external report for GammaVault's collateral calculation. [1](#0-0) [2](#0-1) [3](#0-2) 

### Finding Description
`ChargeAssetTxPayment::withdraw_fee` calls into `T::OnChargeAssetTransaction::withdraw_fee`, whose reference implementation is `SwapAssetAdapter`: [4](#0-3) 

Inside `SwapAssetAdapter::withdraw_fee`, the amount of the user's asset required to cover the native fee is derived exclusively from `quote_price_tokens_for_exact_tokens`, then that exact amount is swapped through the pool via `swap_tokens_for_exact_tokens`: [5](#0-4) 

`quote_price_tokens_for_exact_tokens` reads `balance1`/`balance2` — the pool account's *current* token balances — and feeds them straight into `get_amount_in`, an AMM constant-product formula with no time-weighting, oracle, or manipulation-resistant averaging: [6](#0-5) [7](#0-6) 

The docstring on this function even acknowledges the price is a spot snapshot ("the price may have changed by the time the transaction is executed"), but this note only addresses ordinary slippage between quoting and execution for a *voluntary* swap; it is not a safeguard when the very same reserves are also used to *involuntarily* price a value-transfer (fee payment) that occurs deterministically at `prepare`-time in a `TransactionExtension`, with no `min`/`max` bound supplied by the fee payer.

**Attack primitive:** An attacker who holds the fee-payment asset and also controls the corresponding AMM pool (by trading on it, permissionlessly, like any other user) can, within the same block, submit two of their own extrinsics in nonce order:
1. A large `swap_tokens_for_exact_tokens`/`swap_exact_tokens_for_tokens` call on the pool for `asset_id`/native, shifting `reserve_in` (asset) down and `reserve_out` (native) up.
2. A subsequent extrinsic using `ChargeAssetTxPayment` with the same `asset_id`.

Because pool reserves are ordinary chain state mutated by extrinsic (1) and persisted into extrinsic (2)'s `prepare` step, `get_amount_in` in step (2) computes a reduced `asset_fee` (fewer asset tokens required to buy the same native `fee`), since `amount_in ∝ reserve_in / (reserve_out - amount_out)` and the attacker has just decreased `reserve_in` and increased `reserve_out`. The attacker underpays for their transaction fee at the AMM pool's expense (the pool gives up `fee` amount of native for less-than-fair-value asset input), and can subsequently reverse the initial swap to fully or partially recover the native spent, capturing the difference. This is economically identical to the reported pattern: spot-price read → attacker-induced price shift → asset undervaluation exploited for profit/underpayment, just substituting "collateral valuation" for "fee valuation."

### Impact Explanation
This directly matches the Impact Gate's "public underpriced work that degrades block production" category: the chain's fee mechanism is meant to charge market-equivalent value for the weight/length consumed, but an attacker can systematically underpay fees denominated in AMM-priced assets by self-manipulating the very pool used to price them, at the expense of that pool's liquidity providers (an unbacked value transfer out of the pool). Because `ChargeAssetTxPayment`/`SwapAssetAdapter` is shipped as the reference `OnChargeAssetTransaction` implementation and is enabled on production asset-hub runtimes wherever `pallet-asset-conversion-tx-payment` is configured, any parachain using it inherits this exposure for every non-native fee-asset pool.

### Likelihood Explanation
Likelihood is Medium: it requires no privileged actor, validator, collator, or relayer — only an ordinary signed account that can submit two extrinsics executed in the same block in nonce order (a routine capability on Substrate chains), and sufficient capital/priority to get both extrinsics included together and to move the pool's reserves meaningfully. Thinly-liquid asset/native pools used for fee payment are the most exposed.

### Recommendation
Do not price mandatory fee-asset swaps purely off the pool's instantaneous reserves. Options: (1) use a TWAP-style price accumulated over recent blocks for `quote_price_tokens_for_exact_tokens` when invoked from the fee-payment path, (2) bound the deviation between the quoted price at `validate`/`prepare` time versus a longer-window reference price and reject the extrinsic if deviation exceeds a threshold, or (3) require/allow the fee payer to specify a `max_asset_fee` (slippage bound) so unexpected favorable manipulation cannot be silently exploited by pool state changes made in the same block by the same signer, and add same-block/self-sandwich detection akin to protections already used elsewhere against price manipulation.

### Proof of Concept
1. Attacker funds account `A` with asset `X` and sets up (or targets) an `X`/native pool via `pallet-asset-conversion` with modest liquidity.
2. Block N, extrinsic 1 (nonce k): `A` calls `AssetConversion::swap_exact_tokens_for_tokens` swapping a large amount of native into `X`, pushing pool `reserve_in(X)` down and `reserve_out(native)` up.
3. Block N, extrinsic 2 (nonce k+1): `A` submits any call wrapped with `ChargeAssetTxPayment::from(tip, Some(X))`. `withdraw_fee` → `SwapAssetAdapter::withdraw_fee` → `quote_price_tokens_for_exact_tokens(X, Native, fee, true)` now returns a materially smaller `asset_fee` than it would have pre-manipulation, verifiable analogously to the existing test harness pattern in `transaction_payment_in_asset_possible` (`substrate/frame/transaction-payment/asset-conversion-tx-payment/src/tests.rs:211-300`), by comparing `input_quote` before and after step 2's swap.
4. Block N, extrinsic 3 (nonce k+2, optional): `A` reverses the swap from step 2 to recapture most of the native spent, realizing a net gain equal to the fee underpayment minus swap fees, extracted from the pool's LPs. [8](#0-7)

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

**File:** substrate/frame/asset-conversion/src/lib.rs (L1421-1463)
```rust
		/// Calculates amount in for a given swap `fee`.
		///
		/// Given an output amount of an asset and pair reserves, returns a required input amount
		/// of the other asset.
		pub fn get_amount_in(
			fee: Permill,
			amount_out: &T::Balance,
			reserve_in: &T::Balance,
			reserve_out: &T::Balance,
		) -> Result<T::Balance, Error<T>> {
			let amount_out = T::HigherPrecisionBalance::from(*amount_out);
			let reserve_in = T::HigherPrecisionBalance::from(*reserve_in);
			let reserve_out = T::HigherPrecisionBalance::from(*reserve_out);

			if reserve_in.is_zero() || reserve_out.is_zero() {
				Err(Error::<T>::ZeroLiquidity)?
			}

			if amount_out >= reserve_out {
				Err(Error::<T>::AmountOutTooHigh)?
			}

			let fee_complement = fee.left_from_one().deconstruct();
			let numerator = reserve_in
				.checked_mul(&amount_out)
				.ok_or(Error::<T>::Overflow)?
				.checked_mul(&T::HigherPrecisionBalance::from(Permill::ACCURACY))
				.ok_or(Error::<T>::Overflow)?;

			let denominator = reserve_out
				.checked_sub(&amount_out)
				.ok_or(Error::<T>::Overflow)?
				.checked_mul(&T::HigherPrecisionBalance::from(fee_complement))
				.ok_or(Error::<T>::Overflow)?;

			let result = numerator
				.checked_div(&denominator)
				.ok_or(Error::<T>::Overflow)?
				.checked_add(&One::one())
				.ok_or(Error::<T>::Overflow)?;

			result.try_into().map_err(|_| Error::<T>::Overflow)
		}
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

**File:** substrate/frame/asset-conversion/src/lib.rs (L1523-1547)
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
			};
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

**File:** substrate/frame/transaction-payment/asset-conversion-tx-payment/src/tests.rs (L211-256)
```rust
#[test]
fn transaction_payment_in_asset_possible() {
	let base_weight = 5;
	let balance_factor = 100;
	ExtBuilder::default()
		.balance_factor(balance_factor)
		.base_weight(Weight::from_parts(base_weight, 0))
		.build()
		.execute_with(|| {
			System::set_block_number(1);

			// create the asset
			let asset_id = 1;
			let min_balance = 2;
			assert_ok!(Assets::force_create(
				RuntimeOrigin::root(),
				asset_id.into(),
				42,   // owner
				true, // is_sufficient
				min_balance
			));

			// mint into the caller account
			let caller = 1;
			let beneficiary = <Runtime as system::Config>::Lookup::unlookup(caller);
			let balance = 1000;

			assert_ok!(Assets::mint_into(asset_id.into(), &beneficiary, balance));
			assert_eq!(Assets::balance(asset_id, caller), balance);

			let len = 10;
			let tx_weight = 5;

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
