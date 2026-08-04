## Analysis

The external report's core broken invariant is: **a price is derived from a single, short-window on-chain AMM observation with no resistance to short-term reserve manipulation, and that price is then used to move real value (fee/settlement amount) without any additional safety margin.**

The direct local analog in `paritytech/polkadot-sdk` is `pallet-asset-conversion-tx-payment`'s `SwapAssetAdapter`, which prices transaction fees using the **raw, instantaneous spot reserves** of a `pallet-asset-conversion` liquidity pool — the same underlying primitive Uniswap V3 TWAP is built on top of, except here there is *no* time-weighting or observation window at all; it is a bare one-block spot read.

### Title
Transaction-fee pricing via `SwapAssetAdapter` uses unprotected instantaneous AMM spot price, allowing fee-underpayment/spam through single-block reserve manipulation - (File: `substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs`)

### Summary
`SwapAssetAdapter::withdraw_fee` determines how much of a non-native asset a user must pay for a transaction fee by calling `S::quote_price_tokens_for_exact_tokens`, which resolves to `pallet_asset_conversion::Pallet::quote_price_tokens_for_exact_tokens`. That function reads the pool's *current* on-chain balances via `get_reserves` and plugs them straight into the constant-product formula (`get_amount_in`) with **no time-weighting, no minimum observation window, and no staleness/liquidity-depth check beyond a non-zero balance**. This is the same class of primitive as the Uniswap V3 TWAP used by `TapOracle`, but with an even shorter "window" (a single block/storage read), so the guard the report demands (≥30 min TWAP) does not exist here at all.

### Finding Description [1](#0-0) 
`withdraw_fee` quotes `asset_fee` directly from the live pool price and immediately withdraws that exact amount from the payer — there is no `amount_in_max`/slippage parameter supplied by the extension itself (the extension trusts the quote as authoritative), unlike the pool's own `swap_tokens_for_exact_tokens` extrinsic which lets end users bound slippage.

The underlying quote itself is a bare spot computation: [2](#0-1) 
`quote_price_tokens_for_exact_tokens` calls `Self::get_reserves(asset1, asset2)` (the pool account's *current* balances) and feeds them straight into `get_amount_in`: [3](#0-2) 

There is no accumulator, no multi-block observation, no minimum liquidity/depth threshold, and no protection against the reserves having been skewed moments earlier in the same block. This mirrors exactly the flaw in the report: a critical monetary computation (there: option-exercise price; here: transaction-fee amount) is derived from an AMM pool price with an effectively zero-length averaging window, so anyone able to move the pool's reserves for even a single block can distort the derived price.

### Impact Explanation
An account that also controls (or briefly dominates) liquidity in the fee-asset/native pool can, within a single block and using only its own nonce-ordered sequence of extrinsics:
1. Submit a swap that heavily skews the pool reserves (e.g., inflates the native-side reserve relative to the fee asset reserve).
2. Submit its actual fee-paying extrinsic while the skew is in effect — `SwapAssetAdapter::withdraw_fee` will quote and withdraw a drastically reduced amount of the fee asset for the same native-equivalent fee, because `get_amount_in` scales inversely with the "output" reserve.
3. Reverse the skew afterward, recovering most of the capital used (this pool-internal capital cost is the only cost, exactly as in the referenced Rari Fuse case where the attacker used own capital across a short window rather than a flash loan).

Repeated in this pattern, this becomes a way to submit transactions whose network-fee is priced arbitrarily below its intended native-equivalent — i.e. "public underpriced work" that degrades the anti-spam/fee-based deterrent for block production, and diverts value away from `OU`/block-producer/treasury toward the LPs of the manipulated pool (which absorb the arbitrage loss when price reverts).

### Likelihood Explanation
Likelihood is low-to-moderate, matching the report's own "Low" rating for the Rari-analog scenario: it requires the attacker to hold or borrow(within pool constraints, not flash-loan) enough capital to move the specific fee-asset/native pool meaningfully, and is most practical for low-liquidity pools (e.g., a newly listed asset-hub pool, exactly the "protocol launch" condition called out in the report).

### Recommendation
Do not use the raw instantaneous `get_reserves`/`quote_price_*` spot computation as the authoritative fee-conversion price in `SwapAssetAdapter`. Either:
- Require the extension to accept a caller-supplied `amount_in_max`/slippage bound checked against a smoothed/multi-block reference price, or
- Introduce a time-weighted or multi-block minimum-observation price source (analogous to a TWAP) for fee conversion pools, with a minimum liquidity-depth requirement before a pool is eligible for tx-fee payment, mirroring the report's recommendation of ≥30 minutes of averaging and sufficient pool depth.

### Proof of Concept
1. Create an asset/native pool via `pallet_asset_conversion::create_pool` and seed it with shallow liquidity (as would occur at protocol/asset launch).
2. From the attacker's account, in block N:
   - Extrinsic 1: `swap_tokens_for_exact_tokens`/`swap_exact_tokens_for_tokens` to skew reserves so that `get_amount_in` for the native "fee" leg becomes minimal (inflate the native-side reserve relative to the asset-side reserve).
   - Extrinsic 2: any call using `ChargeAssetTxPayment` with `asset_id = <fee asset>`; observe (as demonstrated by the existing test harness in `substrate/frame/transaction-payment/asset-conversion-tx-payment/src/tests.rs`, e.g. `transaction_payment_without_fee`, which already calls `AssetConversion::quote_price_tokens_for_exact_tokens` directly to compute `fee_in_asset`) that the quoted/withdrawn `fee_in_asset` tracks the skewed reserves rather than any resistant/smoothed price.
   - Extrinsic 3: reverse the skew, recovering capital.
3. Repeat across blocks to sustain underpriced-fee transaction submission, demonstrating degraded fee-based spam resistance for that asset lane. [4](#0-3)

### Citations

**File:** substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs (L142-157)
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
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L1421-1462)
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

**File:** substrate/frame/transaction-payment/asset-conversion-tx-payment/src/tests.rs (L380-386)
```rust
			let input_quote = AssetConversion::quote_price_tokens_for_exact_tokens(
				NativeOrWithId::WithId(asset_id),
				NativeOrWithId::Native,
				fee_in_native,
				true,
			);
			assert_eq!(input_quote, Some(201));
```
