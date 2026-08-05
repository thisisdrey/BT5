### Title
Attacker-manipulable spot-price AMM quote lets `pallet-asset-conversion-tx-payment` charge near-zero real fees, enabling underpriced transaction spam - (File: substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs)

### Summary
The Sherlock report shows that `StaticOracle.quoteAllAvailablePoolsWithTimePeriod` produces a manipulable price because it averages ticks from Uniswap pools regardless of liquidity depth, so a low-liquidity pool can skew the result. The same root cause — using an unprotected, single-block AMM spot price for a security/economic decision, with no minimum-liquidity or TWAP safeguard — exists in `pallet-asset-conversion-tx-payment`, which determines the real transaction-fee amount from the live reserves of a `pallet-asset-conversion` pool that any unprivileged user can create and fully control.

### Finding Description
`SwapAssetAdapter::withdraw_fee` computes how much of a user-chosen `asset_id` must be withdrawn to obtain the fixed native `fee` by calling: [1](#0-0) 

This calls `AssetConversion::quote_price_tokens_for_exact_tokens` → `get_amount_in`, which is a pure spot-price computation over the pool's *current* on-chain reserves: [2](#0-1) 

`pallet-asset-conversion` pools are permissionlessly created (`create_pool`) and permissionlessly funded (`add_liquidity`/`remove_liquidity`/swaps) by any signed account, with no minimum-liquidity threshold enforced on the fee-asset selection path — `quote_price_tokens_for_exact_tokens` only checks that reserves are non-zero, exactly like the audited `StaticOracle._quote` which only requires `_pools.length > 0`: [3](#0-2) 

Because a single account can own 100% of the liquidity of a pool it created, it can deposit a large amount of native token into the pool immediately before submitting a fee-paying extrinsic, skewing the reserve ratio so that `get_amount_in` (asset needed to buy the fixed native `fee`) reports a negligible amount. The subsequent swap in `withdraw_fee` then extracts the required native `fee` amount straight out of the pool — which the attacker itself just funded — using only the attacker's own capital, which can be withdrawn back afterward via `remove_liquidity` or a reverse swap: [4](#0-3) 

`correct_and_deposit_fee` similarly re-quotes the pool at dispatch time for refunds, so the entire fee lifecycle relies solely on the manipulable spot price with no averaging window, no minimum-liquidity gate, and no protection against same-block reserve skewing: [5](#0-4) 

### Impact Explanation
An unprivileged attacker can effectively pay near-zero real economic cost for transactions dispatched with `ChargeAssetTxPayment` against a self-created shallow pool, while `pallet-transaction-payment`'s weight/length-based fee accounting still records a "full" fee as paid (sourced from the attacker's own temporarily deposited liquidity). This lets an attacker spam weight-consuming extrinsics essentially for free, i.e. "public underpriced work that degrades block production," which is explicitly in-scope under the Polkadot SDK Impact Gate. It does not require a malicious validator/collator/relayer — only an ordinary signed account creating and manipulating its own liquidity pool.

### Likelihood Explanation
Creating a pool, funding it, submitting a manipulative sequence of extrinsics in the same or adjacent blocks, and withdrawing liquidity afterward are all permissionless, low-cost operations available to any account holding minimal native balance and no special privileges. The only "cost" is the round-trip swap fee on the attacker's own pool (which they can also set to `0` if it is a per-pool fee they created, subject to `set_pool_fee` permissions) — making repeated exploitation economically attractive versus paying full transaction fees.

### Recommendation
- Require the fee-asset pool used by `ChargeAssetTxPayment`/`SwapAssetAdapter` to satisfy a minimum-liquidity or maximum price-impact threshold before it is eligible for fee payment.
- Use a time-weighted or multi-block average price (analogous to the TWAP protections available for Uniswap V3) rather than the instantaneous spot reserve ratio for fee quoting.
- Consider restricting or additionally rate-limiting which pools/assets are eligible as `AssetId` for `ChargeAssetTxPayment`, or bound the allowed slippage/deviation between the quoted fee and a longer-window reference price.

### Proof of Concept
1. Attacker creates asset `X` and a pool `(Native, X)` via `AssetConversion::create_pool`, becoming sole liquidity provider with minimal starting liquidity.
2. Attacker submits extrinsic A: a large `swap_exact_tokens_for_tokens` depositing a large amount of `Native` into the pool (self-funded), which inflates the `Native` reserve relative to the `X` reserve.
3. In the same block, attacker submits extrinsic B: any weight-heavy call using `ChargeAssetTxPayment` with `asset_id = X`. `withdraw_fee` calls `quote_price_tokens_for_exact_tokens(X, Native, fee, true)` (`substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs:142-146`), which — given the skewed reserves — returns a near-zero `asset_fee` in `X`; the swap in the same call then extracts the full native `fee` from the pool (the attacker's own deposited native), so the attacker pays almost nothing in real value for the dispatched call.
4. Attacker submits extrinsic C: withdraws/reverses the liquidity/swap to reclaim the native tokens deposited in step 2, closing the loop at near-zero net cost.
5. Repeating this pattern lets the attacker dispatch arbitrary weight-heavy calls for negligible real cost, consuming block space cheaply.

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

**File:** substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs (L259-266)
```rust
		// refund is non zero and `who`'s fee `asset_id` is not the target asset.

		// check if the refund amount can be swapped back into `who`'s fee `asset_id`.
		let refund_asset_amount =
			S::quote_price_exact_tokens_for_tokens(A::get(), asset_id.clone(), refund_amount, true)
				// No refund given if it cannot be swapped back.
				.unwrap_or(Zero::zero());

```

**File:** substrate/frame/asset-conversion/src/lib.rs (L1425-1463)
```rust
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
