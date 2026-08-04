This is a solid local analog: `pallet-asset-conversion`'s spot-price `quote_price_tokens_for_exact_tokens`/`quote_price_exact_tokens_for_tokens` functions are used directly (no TWAP) as the price oracle for paying transaction fees in a non-native asset in `pallet-asset-conversion-tx-payment`.

### Title
Transaction fees paid in non-native assets are priced from a manipulable spot AMM reserve, allowing fee underpayment via same-block sandwich swaps - (File: substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs)

### Summary
`pallet-asset-conversion-tx-payment`'s `SwapAssetAdapter::withdraw_fee`/`can_withdraw_fee` convert the native transaction fee into an equivalent amount of a user-chosen fee asset by calling `S::quote_price_tokens_for_exact_tokens`, which is implemented directly against the live pool reserves (`get_reserves`) of `pallet-asset-conversion`. There is no TWAP or other manipulation-resistant pricing, exactly the root cause identified in the external report (`CURVE_POOL.get_virtual_price()` on-chain spot price used as an oracle). Because the pool reserves can be temporarily skewed by ordinary, unprivileged swap extrinsics executed earlier in the same block, an attacker can cheapen the asset-denominated fee charged for their own transaction, then reverse the swap afterward.

### Finding Description
`withdraw_fee` computes the fee-asset amount as: [1](#0-0) 
using `S::quote_price_tokens_for_exact_tokens`, which for `pallet-asset-conversion` resolves to `Pallet::<T>::quote_price_tokens_for_exact_tokens`, itself built from the pool's *current* on-chain balances: [2](#0-1) [3](#0-2) 

The pallet's own `QuotePrice` trait documents this exact hazard without offering any mitigation: [4](#0-3) 

`get_amount_in`/`get_amount_out` are pure constant-product formulas over `reserve_in`/`reserve_out` taken at call time, with no time-weighting or moving-average smoothing: [5](#0-4) 

Because the quote and the withdrawal both execute atomically within a single extrinsic (`prepare`/`withdraw_fee`), the vulnerability is not inside that one call — it is that nothing prevents an attacker from placing an ordinary swap extrinsic of their own immediately *before* their fee-paying extrinsic within the same block to skew `reserve_in`/`reserve_out` in their favor, and a reversing swap immediately *after* to restore the pool and recoup most of the capital. As an unprivileged user who only submits their own regular extrinsics (no malicious validator, collator, relayer, or governance actor required), the attacker can transiently make their chosen fee-asset "cheap" relative to the native fee asset at the exact block-execution moment their transaction is processed, so `quote_price_tokens_for_exact_tokens` returns an artificially small `asset_fee` for a fixed native `fee`.

### Impact Explanation
The chain still grants the attacker's transaction its full weight/length allocation and treats the fee as paid in full, but the actual value extracted from the attacker (in the fee-receiving asset, ultimately convertible back to native value) is less than the intended fee. This is a "public underpriced work" condition: an unprivileged actor can systematically under-pay for block resources by manipulating the on-chain AMM oracle used to price fees, degrading fee-revenue integrity and enabling cheap congestion of block space by heavy transactions paid through manipulated pools. This directly matches the accepted impact class of "public underpriced work that degrades block production."

### Likelihood Explanation
Likelihood is Low/Medium-dependent on pool depth and AMM fee rate: exploitation requires the profit from the fee discount to exceed the round-trip AMM swap fee (typically 0.3%) plus any slippage/other pool activity in the same block, so it is most attractive on transactions with large weight/length fees relative to a shallow liquidity pool. No privileged access, validator collusion, or off-chain infrastructure is required — only ordinary extrinsic submission and control over intra-block ordering via normal fee/priority bidding, which any user can attempt.

### Recommendation
Do not price fee-asset conversion from the instantaneous pool reserves alone. Introduce a manipulation-resistant reference price (e.g., a block-count or time-weighted average of reserves/price sampled over multiple blocks) for use in `SwapAssetAdapter::withdraw_fee`/`can_withdraw_fee`, or bound the deviation between the spot quote and a recent moving average, rejecting/falling back to native-fee payment when the deviation exceeds a safe threshold.

### Proof of Concept
1. Attacker holds asset `X` and wants to pay transaction fees in `X` via `ChargeAssetTxPayment` (`asset_id = X`).
2. In the same block, before their target transaction, the attacker submits a large `swap_exact_tokens_for_tokens`/`swap_tokens_for_exact_tokens` extrinsic that shifts the `X`/native pool reserves so that `X` becomes cheap relative to native at that pool state.
3. The attacker's fee-paying transaction executes next; `withdraw_fee` calls `quote_price_tokens_for_exact_tokens(X, Native, fee, true)` against the now-skewed reserves (`substrate/frame/asset-conversion/src/lib.rs:1571-1603`), returning a much smaller `asset_fee` than would be quoted against the pool's un-manipulated equilibrium reserves.
4. The attacker's transaction is charged only this reduced `asset_fee` amount of `X` (`substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs:142-157`) while still receiving full weight/length allowance.
5. In a subsequent extrinsic in the same block, the attacker reverses the initial swap, restoring the pool and recovering most of the capital used to skew it, net of the round-trip AMM fee.

### Citations

**File:** substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs (L142-146)
```rust
		// Quote the amount of the `asset_id` needed to pay the fee in the asset `A`.
		let asset_fee =
			S::quote_price_tokens_for_exact_tokens(asset_id.clone(), A::get(), fee, true)
				.filter(|asset_fee| !asset_fee.is_zero())
				.ok_or(InvalidTransaction::Payment)?;
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

**File:** substrate/frame/asset-conversion/src/swap.rs (L116-120)
```rust
/// Trait providing methods to quote swap prices between asset classes.
///
/// The quoted price is only guaranteed if no other swaps are made after the price is quoted and
/// before the target swap (e.g., the swap is made immediately within the same transaction).
pub trait QuotePrice {
```
