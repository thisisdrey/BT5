## Analysis

The GMX report's core problem is: **a price used to determine execution/value comes from an instantaneous ("spot") read of a market that a party can move within one atomic window, with no TWAP/multi-reading smoothing and no deviation cap.**

The closest local analog in `Loderfordw/polkadot-sdk--031` is not the new `pallet-oracle` (that pallet is permissioned — only `Members`-approved feeders can post data, so manipulating it needs a "malicious oracle operator," which is out of scope per the impact gate). The real, unprivileged-attacker-reachable analog is `pallet-asset-conversion-tx-payment`'s `SwapAssetAdapter`, which prices transaction fees paid in a non-native asset using the **live, single-pool AMM spot price** of `pallet-asset-conversion`, with no TWAP and no deviation guard.

### Title
Transaction-fee asset pricing uses unprotected single-pool AMM spot price, enabling atomic self-manipulation to underpay fees at LP expense - (File: `substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs`)

### Summary
`SwapAssetAdapter::withdraw_fee` determines how much of a non-native asset a signed account must pay for a transaction fee by calling `S::quote_price_tokens_for_exact_tokens`, which reads the AMM pool's *current* reserves (`Self::get_reserves`) and applies the constant-product formula (`get_amount_in`) with no time-weighting, no minimum number of price observations, and no cap on price deviation between blocks/extrinsics. [1](#0-0) 

### Finding Description
`quote_price_tokens_for_exact_tokens` computes the swap price purely from the pool's instantaneous balances at call time: [2](#0-1) 

The underlying pricing function `get_amount_in` is a straightforward constant-product formula with no smoothing: [3](#0-2) 

`withdraw_fee` uses this instantaneous quote to decide the exact amount of the non-native asset to withdraw from the signer, then swaps it for the native fee amount at whatever the pool state is at that moment — with no slippage bound elected by (or protecting) the fee payer: [4](#0-3) 

An unprivileged signer, submitting several of their own extrinsics with sequential nonces (which the transaction pool normally orders and typically includes in the same block), can:
1. Swap native → asset X in the pool to shrink `reserve_X` relative to `reserve_native` (pumping X's quoted price).
2. Submit the fee-paying transaction (using asset X to pay fees) while the pool is in this skewed state; `withdraw_fee`'s quote returns an artificially reduced amount of X needed to cover the fixed native-denominated fee.
3. Swap X back for native to restore/approximately restore the pool, absorbing only the pool's normal LP fee on the round trip.

Because the fee-conversion path has no TWAP, no minimum-liquidity/price-deviation check, and no per-fee-payment slippage limit (unlike ordinary `swap_exact_tokens_for_tokens` calls, which do accept `amount_out_min`/`amount_in_max` from the caller), the attacker can extract value from the pool's liquidity providers whenever the fee-native-amount's price impact exceeds the round-trip AMM fee cost — exactly the "economically viable price manipulation" scenario the external report warns about, just localized to a single on-chain AMM pool instead of multiple exchanges.

### Impact Explanation
This directly hits the "public underpriced work" / value-conservation pivot: transaction fees are meant to be paid at a fair market rate, and the swap that funds the fee is meant to conserve value for LPs. Repeated exploitation drains the AMM pool (griefing LPs, i.e., "theft... unbacked... duplicate settlement" class value loss) while requiring nothing but ordinary signed transactions from an unprivileged account — no validator, collator, relayer, or governance compromise.

### Likelihood Explanation
Likelihood is bounded by: (a) pool size relative to the fee amount — small/thin pools (more likely for newly listed assets accepted for fee payment) magnify price impact; (b) whether an attacker's own sequential-nonce extrinsics land in the same block, which is the normal case for a single signer submitting a small burst of transactions on a lightly congested chain. No special relayer/validator collusion is required, making this plausible in practice, particularly against low-liquidity fee-asset pools that runtimes choose to whitelist for this feature.

### Recommendation
- Do not price fee-asset conversion from an instantaneous pool read; use a time-weighted average price (TWAP) over multiple blocks, or bound the acceptable price deviation from a recent reference price before allowing the fee-asset swap.
- Alternatively, cap the fee-asset amount using a runtime-configured maximum deviation from a moving average, and/or require minimum pool liquidity relative to the fee size before accepting an asset for fee payment.
- Consider rejecting fee-asset payment when the quoted price differs beyond a configurable threshold from the price observed N blocks earlier (open-interest-cap-style failsafe, as recommended in the source report).

### Proof of Concept
Conceptual sequence (single signer, single block, sequential nonces):
1. `swap_exact_tokens_for_tokens(path=[Native, X], amount_in=<large>, ...)` — pumps `reserve_native` up / `reserve_X` down in the `X/Native` pool.
2. Submit any call with `ChargeAssetTxPayment::from(tip, Some(X))` — `withdraw_fee` calls `quote_price_tokens_for_exact_tokens(X, Native, fee_in_native, true)` against the now-skewed reserves, withdrawing fewer X tokens than the pre-manipulation price would require. [5](#0-4) 
3. `swap_exact_tokens_for_tokens(path=[X, Native], amount_in=<same large amount from step 1>, ...)` — restores the pool, paying only the constant-product LP fee on the round trip.

Net effect: attacker's fee cost is reduced below fair market value; the deficit is absorbed by the pool's liquidity providers, exactly mirroring the "spot price manipulation for guaranteed profit" pattern described in the external GLOBAL-4 report, but realized against `pallet-asset-conversion`'s single-pool spot price with no TWAP protection in `pallet-asset-conversion-tx-payment`.

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
