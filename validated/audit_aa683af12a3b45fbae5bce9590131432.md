## Finding

### Title
Transaction-fee-in-asset extension prices swaps off manipulable AMM spot reserves - ([File: substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs])

### Summary
`SwapAssetAdapter::withdraw_fee` and `correct_and_deposit_fee` price the mandatory native-fee↔asset swap using `pallet_asset_conversion`'s `quote_price_tokens_for_exact_tokens`/`quote_price_exact_tokens_for_tokens`, which read the pool's *current* token balances (`get_reserves`) as the spot price with no TWAP or manipulation-resistant oracle — the same class of bug as the reported `_sqrtPriceX96FromPoolAndInterval` issue that reads `globalState()` instead of a time-weighted price.

### Finding Description
`pallet-asset-conversion::get_reserves` simply returns the live balances held by the pool account: [1](#0-0) 

`quote_price_tokens_for_exact_tokens`/`quote_price_exact_tokens_for_tokens` compute a swap price directly from those instantaneous reserves with no averaging or staleness protection: [2](#0-1) 

`SwapAssetAdapter::withdraw_fee` (used by `ChargeAssetTxPayment`, the standard "pay fees in any asset" transaction extension) uses exactly this instantaneous quote to decide how much of the payer's asset to debit for a fixed native fee, then performs the actual swap at that quoted amount with no user-supplied slippage bound: [3](#0-2) 

`correct_and_deposit_fee` repeats the pattern for the post-dispatch refund, re-quoting from the pool state *after* the wrapped call has executed: [4](#0-3) 

Because the price source is the pool's live balance rather than a manipulation-resistant reference, an ordinary signed account can:
1. Submit extrinsic #1 from its own account: a large `pallet_asset_conversion::swap_exact_tokens_for_tokens` that skews the reserves of the `asset_id`/native pool in its favor (cheapens `asset_id` relative to native in the pool's internal accounting).
2. Submit extrinsic #2 in the same block: a call wrapped by `ChargeAssetTxPayment` paying fees in `asset_id`. `withdraw_fee` quotes `asset_fee` off the now-skewed reserves, so the attacker is debited an artificially small amount of `asset_id` for the same native `fee`, while the swap that actually executes still extracts the true native `fee` amount from the pool's liquidity — the difference is absorbed by LPs, not the attacker.
3. The attacker (or an accomplice trade in a later block) reverses the initial skew, realizing the extracted value while LPs bear the loss.

The same manipulation applies in reverse to the post-dispatch refund quote in `correct_and_deposit_fee`, since the wrapped `T::RuntimeCall` itself can be an AMM swap that shifts reserves between the pre-dispatch and post-dispatch quotes within one atomic transaction — giving the attacker control over both price snapshots used to compute their own refund.

Existing guards do not stop this: swap extrinsics normally require a caller-supplied `amount_out_min`/`amount_in_max` to bound slippage, but the tx-payment integration computes and executes swaps automatically with no such bound, and nothing in `get_reserves`/`quote_price_*` averages price over time or checks for large single-block reserve deltas.

### Impact Explanation
The corrupted value is the AMM pool's reserve-derived spot price used as the exchange rate for fee payment and fee refund. Any account able to submit an ordinary swap plus a fee-paying extrinsic in the same block (no validator, relayer, or admin privilege needed) can bias this price to under-pay fees or over-collect refunds at the liquidity providers' expense, violating the value-conservation requirement that fee-asset conversions settle at a fair, non-manipulable rate.

### Likelihood Explanation
High: this requires only an unprivileged signed account able to submit two ordinary, already-supported extrinsics (a swap and a fee-in-asset transaction) within the same block — no special node, peer, or governance access. `pallet-asset-conversion` pools are permissionless to trade against, so any pair with an `asset_id`/native pool used for fee payment is exposed.

### Recommendation
Do not let `SwapAssetAdapter`/`ChargeAssetTxPayment` execute swaps priced purely off instantaneous `get_reserves`. Either (a) require a caller-supplied maximum acceptable `asset_id` cost / minimum refund (i.e., real slippage bounds) enforced against the quoted price, or (b) source the exchange rate from a manipulation-resistant reference (e.g., an accumulator/TWAP over the pool's reserves, or a bounded maximum deviation from a recent block's price) before using it to withdraw or refund fees.

### Proof of Concept
1. Create/observe an existing `asset_id`/native `pallet_asset_conversion` pool with liquidity `L1`/`L2`.
2. From attacker account `A`, in block `N`, submit extrinsic 1: `swap_exact_tokens_for_tokens([asset_id, Native], large_amount_in, 0, A, false)` — this skews the pool balances so that `quote_price_tokens_for_exact_tokens(asset_id, Native, fee, true)` (see `substrate/frame/asset-conversion/src/lib.rs:1571-1603`) now returns an artificially low `asset_fee`.
3. In the same block, submit extrinsic 2: any call wrapped with `ChargeAssetTxPayment::from(tip, Some(asset_id))`. `withdraw_fee` (`payment.rs:142-157`) debits `A` only the artificially low `asset_fee`, while the internal swap still pays the pool the full native `fee`.
4. In block `N+1` (or a later swap), `A` reverses the initial skew, realizing the LP-funded discount as profit.
5. Compare `A`'s net `asset_id` balance change against the fair-price cost computed from pre-manipulation reserves to demonstrate the discount extracted from LPs.

### Citations

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

**File:** substrate/frame/asset-conversion/src/lib.rs (L1564-1603)
```rust
		/// Gets a quote for swapping `amount` of `asset1` for an exact amount of `asset2`.
		///
		/// If `include_fee` is true, the quote will include the liquidity provider fee.
		/// If the pool does not exist or has no liquidity, `None` is returned.
		/// Note that the price may have changed by the time the transaction is executed.
		/// (Use `amount_in_max` to control slippage.)
		/// Returns `Some(quoted_amount)` on success.
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

**File:** substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs (L142-170)
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
```

**File:** substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs (L259-266)
```rust
		// refund is non zero and `who`'s fee `asset_id` is not the target asset.

		// check if the refund amount can be swapped back into `who`'s fee `asset_id`.
		let refund_asset_amount =
			S::quote_price_exact_tokens_for_tokens(A::get(), asset_id.clone(), refund_amount, true)
				// No refund given if it cannot be swapped back.
				.unwrap_or(Zero::zero());

```
