## Title
Instantaneous AMM reserve manipulation lets fee payers underpay transaction fees via `pallet-asset-conversion` price quotes - (File: `substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs`)

### Summary
The Aloe bug stems from a risk parameter (implied volatility) being derived from an **instantaneous, single-block snapshot of on-chain liquidity depth** that any account can freely push in one direction and then revert, at essentially zero cost beyond gas. The local analog in this repository is `SwapAssetAdapter::withdraw_fee`/`can_withdraw_fee` in `pallet-asset-conversion-tx-payment`, which prices a transaction's fee (paid in a non-native asset) using `QuotePrice::quote_price_tokens_for_exact_tokens`, which in turn reads `pallet_asset_conversion::Pallet::get_reserves` — the **live spot balances of the fee-asset/native pool at the moment the extrinsic is validated**, exactly analogous to Aloe's single-block `tickTvl` read.

### Finding Description
`SwapAssetAdapter::withdraw_fee` computes how much of a user-chosen `asset_id` to debit for a transaction's native-denominated fee by calling: [1](#0-0) 

This delegates to `quote_price_tokens_for_exact_tokens`, which reads the pool's *current* reserves via `get_reserves`/`get_balance` and plugs them straight into the constant-product formula `get_amount_in`: [2](#0-1) [3](#0-2) 

The comment on `QuotePrice` explicitly acknowledges the quote is only valid "if no other swaps are made after the price is quoted", i.e. it is a spot read of mutable state with no time-weighting or manipulation resistance: [4](#0-3) 

Because reserves are ordinary pallet-assets/pallet-balances balances of the pool account, any account can shift the reserve ratio within the same block sequence by swapping through the pool (`swap_exact_tokens_for_tokens`/`swap_tokens_for_exact_tokens`), pushing the `asset_id` reserve down relative to the native reserve. A subsequent extrinsic from the same signer, submitted later in the same block (guaranteed by nonce ordering), pays its transaction fee in `asset_id` at the now-skewed price computed by `withdraw_fee`, then a follow-up swap reverses the skew. This mirrors the Aloe pattern of deposit → read → withdraw, except here the state is pool reserves feeding a fee-pricing oracle instead of tick liquidity feeding an IV oracle. `pallet_asset_conversion::PoolFees` is a fixed `Permill` set per pool and does not scale with trade size or attenuate large skewing swaps, so unlike a real DEX arbitrage constraint, the "cost" of the manipulation is bounded to that flat swap fee rather than growing with slippage protection designed for this specific attack surface — there is no TWAP, no minimum-liquidity-weighted average, and no cooldown between reserve state and fee quoting, so `IV_CHANGE_PER_UPDATE`-style throttling (which Aloe *did* have and which Sherlock still rated High) is entirely absent here.

### Impact Explanation
This directly matches the "public underpriced work that degrades block production" impact category: the transaction-fee mechanism exists to price block weight/length fairly; an attacker who can transiently cheapen the quoted `asset_id` cost of a native fee is able to submit weight-consuming extrinsics for less than their fair-value cost, repeatedly, degrading the fee market's ability to price and rate-limit block space. It also generalizes to any consumer of `pallet_asset_conversion`'s view functions that trusts a single-block quote as ground truth (e.g. `PoolAdapter::quote_price_tokens_for_exact_tokens` used by asset-hub runtime APIs, and the `xcm-builder` `SingleAssetExchangeAdapter`), any of which could be fed a manipulated price for the duration of one block.

### Likelihood Explanation
Any signed account with modest capital in the two assets of a pool can perform this without governance, validator, relayer, or leaked-key involvement — purely through ordinary signed extrinsics (`swap_*`, then a fee-paying call, then a reversing `swap_*`) within a block they control the ordering of via nonces. The only cost is the flat `PoolFee` (typically well under 1%) on the temporary swap volume, which can be far smaller than the fee savings achieved on the targeted extrinsic(s) if repeated or scaled, making this a low-cost, unprivileged, repeatable manipulation rather than a one-off arbitrage.

### Recommendation
Do not use a single-block spot reserve read to price transaction fees. Use a time-weighted average of pool reserves/price (analogous to the report's recommendation to use time-weighted average liquidity) with a minimum observation window before a quote can be used in `withdraw_fee`/`can_withdraw_fee`, or bound the fee-asset amount by the previous block's quoted price plus a small tolerance, so intra-block reserve swings cannot be exploited to underpay for the same block's own transaction.

### Proof of Concept
1. Attacker holds `asset_id` and native currency, and a pool for `(asset_id, Native)` exists with reserves `R_a`, `R_n`.
2. Extrinsic 1 (attacker, nonce N): `AssetConversion::swap_exact_tokens_for_tokens` swapping a large amount of `Native` into the pool for `asset_id`, draining `asset_id` reserve down to `R_a' << R_a` (cost: flat pool fee only).
3. Extrinsic 2 (attacker, nonce N+1, same block): any weight-heavy call, fee paid with `asset_id`. `ChargeAssetTxPayment`'s extension calls `SwapAssetAdapter::withdraw_fee`, which calls `quote_price_tokens_for_exact_tokens(asset_id, Native, fee_in_native, true)` against the now-skewed reserves `R_a'`, `R_n'`, producing a much smaller `asset_fee` than the fair-value quote against `R_a`, `R_n` — see the formula in `get_amount_in`: [5](#0-4) 
4. Extrinsic 3 (attacker, nonce N+2, same block): reverse swap restoring `R_a`, `R_n`, recovering nearly all capital except the pool fee.
5. Net effect: the attacker's weight-heavy extrinsic (2) was charged an `asset_id` amount that undervalues the true native-fee cost of the block weight/length it consumed, for a total cost of two flat-fee swaps rather than the correct fee.

### Citations

**File:** substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs (L142-146)
```rust
		// Quote the amount of the `asset_id` needed to pay the fee in the asset `A`.
		let asset_fee =
			S::quote_price_tokens_for_exact_tokens(asset_id.clone(), A::get(), fee, true)
				.filter(|asset_fee| !asset_fee.is_zero())
				.ok_or(InvalidTransaction::Payment)?;
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

**File:** substrate/frame/asset-conversion/src/lib.rs (L1571-1602)
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
```

**File:** substrate/frame/asset-conversion/src/swap.rs (L116-120)
```rust
/// Trait providing methods to quote swap prices between asset classes.
///
/// The quoted price is only guaranteed if no other swaps are made after the price is quoted and
/// before the target swap (e.g., the swap is made immediately within the same transaction).
pub trait QuotePrice {
```
