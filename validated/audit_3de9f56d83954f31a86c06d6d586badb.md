## Title
Fee-currency conversion in `pallet-asset-conversion-tx-payment` relies on the manipulable, unrestricted spot price of `pallet-asset-conversion` pools, letting an unprivileged account under-pay transaction fees within a single block - (`File: substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs`)

### Summary
The Omnipool report shows that an exchange rate computed from a pool's instantaneous reserves can be manipulated within one block because the pool's guard rail (a per-account "no deposit+withdraw in the same block" check) can be bypassed by moving the transferable share token to a second account. The structural weakness — a value-critical price read taken from freely-mutable, unrestricted AMM reserves, with no protection against intra-block manipulation — has a direct analog in `pallet-transaction-payment/asset-conversion-tx-payment`, which prices transaction fees using the live spot price of a `pallet-asset-conversion` pool. Unlike the Omnipool contract, `pallet-asset-conversion` has *no* same-block cooldown at all on `add_liquidity`/`remove_liquidity`/swaps, so any account can shift the pool ratio and restore it within the same block, and the fee-payment extension will read whatever reserve state exists at the moment of the fee-paying extrinsic.

### Finding Description
`SwapAssetAdapter::withdraw_fee` in [1](#0-0)  computes the amount of the user's chosen asset needed to pay a fee by calling `S::quote_price_tokens_for_exact_tokens(asset_id, A::get(), fee, true)`, which is implemented on top of `pallet_asset_conversion::Pallet::quote_price_tokens_for_exact_tokens`, itself derived from the pool's *current* reserves: [2](#0-1) .

The reserves that back this quote are trivially mutable in the same block: `do_add_liquidity` and `do_remove_liquidity` mint/burn a fully transferable LP token and move balances based on whatever `reserve1`/`reserve2` happen to be at call time, with no time-based or per-account restriction at all: [3](#0-2)  and [4](#0-3) . Similarly, `swap_exact_tokens_for_tokens`/`swap_tokens_for_exact_tokens` can shift the ratio arbitrarily within the same block, and can be reversed with a second swap in the same block, since there is no cooldown analogous to Omnipool's `lastTransactionBlock` check.

Because a single account can, within one block, (1) submit a large swap or add/remove-liquidity call that skews the pool ratio in the direction that minimizes `quote_price_tokens_for_exact_tokens(asset_id, native, fee, true)`, (2) immediately submit the fee-bearing extrinsic (using `ChargeAssetTxPayment`) which withdraws and swaps only the manipulated, artificially low amount of `asset_id` via `S::swap_tokens_for_exact_tokens` in the same call: [5](#0-4) , and (3) reverse the skew with a following swap in the same block — the attacker pays the chain a manipulated (under-priced) real-value fee for their transaction weight/length, while extracting the corresponding value from the pool's other liquidity providers via the swap round-trip. No malicious collator, validator, or governance actor is needed: this is achievable purely through the attacker's own sequence of extrinsics landing in the same block (e.g. via tip/priority), analogous to how the Omnipool PoC used two of the attacker's own accounts within one block.

### Impact Explanation
This directly matches the "public underpriced work that degrades block production" and "asset accounting" impact categories: the chain systematically under-collects transaction fees (paid in a non-native asset) relative to their intended native-asset value, while other liquidity providers in the pool absorb the corresponding loss. At scale this both degrades the economic security of block-space pricing and constitutes value extraction from LPs without their consent, mirroring the "manipulate exchangerate" impact of the seed report.

### Likelihood Explanation
Likelihood is High for chains that enable `pallet-asset-conversion-tx-payment` with pools that have realistic (non-infinite) depth: any unprivileged, unprivileged account can execute the 3-step sequence entirely with its own keys and extrinsics, and only needs same-block inclusion of its own transactions (achievable through fee/tip-based ordering), not collusion with block producers.

### Recommendation
Do not let `withdraw_fee`/`can_withdraw_fee` price the fee purely off the pool's instantaneous reserves within the same block as the fee-payer's own preceding swap/liquidity actions. Options: use a time-weighted average price (TWAP) or a price staleness/deviation check as the basis for fee quoting, require the quoted price to be bounded against a slippage limit supplied by the runtime rather than the caller, or otherwise decouple the fee-asset conversion rate from a pool the fee-payer can move immediately beforehand in the same block.

### Proof of Concept
Conceptual (Substrate) sequence within a single block, executed by a single attacker-controlled account `A` holding both `asset_id` and enough of `asset_id`/native to swap:
1. `A` calls `AssetConversion::swap_exact_tokens_for_tokens` (or `add_liquidity`) to shift the `asset_id`/native pool ratio so that `quote_price_tokens_for_exact_tokens(asset_id, native, fee, true)` returns an artificially small `asset_fee`.
2. `A` submits any extrinsic with `ChargeAssetTxPayment::from(tip, Some(asset_id))`; `withdraw_fee` (`substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs:142-176`) withdraws only the manipulated `asset_fee` and swaps it for the exact native `fee` using the now-skewed pool.
3. `A` submits a reversing swap (or `remove_liquidity`) restoring the pool close to its original ratio, net-extracting the LP-side value lost in steps 1–2 while having paid a fee far below its intended native value.

This mirrors the existing test harness pattern already in the repo (`setup_lp`, `quote_price_tokens_for_exact_tokens`, `ChargeAssetTxPayment::validate_and_prepare`) shown in [6](#0-5) , but adds attacker-controlled pool-skewing swaps immediately before/after the fee-paying extrinsic within the same block.

### Citations

**File:** substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs (L142-146)
```rust
		// Quote the amount of the `asset_id` needed to pay the fee in the asset `A`.
		let asset_fee =
			S::quote_price_tokens_for_exact_tokens(asset_id.clone(), A::get(), fee, true)
				.filter(|asset_fee| !asset_fee.is_zero())
				.ok_or(InvalidTransaction::Payment)?;
```

**File:** substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs (L159-176)
```rust
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

**File:** substrate/frame/asset-conversion/src/lib.rs (L813-856)
```rust
			let reserve1 = Self::get_balance(&pool_account, asset1.clone());
			let reserve2 = Self::get_balance(&pool_account, asset2.clone());

			let amount1: T::Balance;
			let amount2: T::Balance;
			if reserve1.is_zero() || reserve2.is_zero() {
				amount1 = amount1_desired;
				amount2 = amount2_desired;
			} else {
				let amount2_optimal = Self::quote(&amount1_desired, &reserve1, &reserve2)?;

				if amount2_optimal <= amount2_desired {
					ensure!(
						amount2_optimal >= amount2_min,
						Error::<T>::AssetTwoDepositDidNotMeetMinimum
					);
					amount1 = amount1_desired;
					amount2 = amount2_optimal;
				} else {
					let amount1_optimal = Self::quote(&amount2_desired, &reserve2, &reserve1)?;
					ensure!(
						amount1_optimal <= amount1_desired,
						Error::<T>::OptimalAmountLessThanDesired
					);
					ensure!(
						amount1_optimal >= amount1_min,
						Error::<T>::AssetOneDepositDidNotMeetMinimum
					);
					amount1 = amount1_optimal;
					amount2 = amount2_desired;
				}
			}

			ensure!(
				amount1.saturating_add(reserve1) >= T::Assets::minimum_balance(asset1.clone()),
				Error::<T>::AmountOneLessThanMinimal
			);
			ensure!(
				amount2.saturating_add(reserve2) >= T::Assets::minimum_balance(asset2.clone()),
				Error::<T>::AmountTwoLessThanMinimal
			);

			T::Assets::transfer(asset1, who, &pool_account, amount1, Preserve)?;
			T::Assets::transfer(asset2, who, &pool_account, amount2, Preserve)?;
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L909-920)
```rust
			let pool = Pools::<T>::get(&pool_id).ok_or(Error::<T>::PoolNotFound)?;

			let pool_account =
				T::PoolLocator::address(&pool_id).map_err(|_| Error::<T>::InvalidAssetPair)?;
			let (reserve1, reserve2) = Self::get_reserves(asset1.clone(), asset2.clone())?;

			let total_supply = T::PoolAssets::total_issuance(pool.lp_token.clone());
			let withdrawal_fee_amount = T::LiquidityWithdrawalFee::get() * lp_token_burn;
			let lp_redeem_amount = lp_token_burn.saturating_sub(withdrawal_fee_amount);

			let amount1 = Self::mul_div(&lp_redeem_amount, &reserve1, &total_supply)?;
			let amount2 = Self::mul_div(&lp_redeem_amount, &reserve2, &total_supply)?;
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

**File:** substrate/frame/transaction-payment/asset-conversion-tx-payment/src/tests.rs (L211-277)
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

			let (pre, _) = ChargeAssetTxPayment::<Runtime>::from(0, Some(asset_id.into()))
				.validate_and_prepare(
					Some(caller).into(),
					CALL,
					&info_from_weight(WEIGHT_5),
					len,
					0,
				)
				.unwrap();
			// assert that native balance is not used
			assert_eq!(Balances::free_balance(caller), 10 * balance_factor);

			// check that fee was charged in the given asset
			assert_eq!(Assets::balance(asset_id, caller), balance - fee_in_asset);

			System::assert_has_event(RuntimeEvent::Assets(pallet_assets::Event::Withdrawn {
				asset_id,
				who: caller,
				amount: fee_in_asset,
			}));
```
