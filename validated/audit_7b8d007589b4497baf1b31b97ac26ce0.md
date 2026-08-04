Based on my research, I found a solid local analog that matches the OracleMaker bug class: an internal, security-relevant price/rate is derived from a live, single-block-manipulable on-chain balance (AMM pool reserves) with no depth/TWAP protection, and is used to determine how much value a user must forfeit in a "forced" internal swap. Just like OracleMaker's premium being deflated by transient free collateral, `pallet-asset-conversion-tx-payment`'s `SwapAssetAdapter::withdraw_fee` prices a mandatory fee-conversion swap off the pool's instantaneous spot reserves, which the fee-payer (if also an LP of that pool) can skew in a preceding extrinsic in the same block, then restore afterward — extracting value from the pool's other LPs while still fully satisfying the chain's fixed native fee requirement.

### Title
Fee-asset swap for transaction payment prices off manipulable spot reserves with no slippage bound - ([File: substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs])

### Summary
`SwapAssetAdapter::withdraw_fee` converts a user's chosen fee asset into the native fee asset by quoting `pallet-asset-conversion`'s **current pool reserves** (spot price) and then performing an internal swap for the exact quoted amount. There is no minimum-liquidity/TWAP protection and no user- or protocol-imposed slippage bound distinct from the quote itself. A liquidity provider who is also the fee payer can transiently skew the pool ratio (via `remove_liquidity`/`add_liquidity`) in an earlier extrinsic of the same block, causing the subsequent fee-quote to undervalue the amount of `asset_id` required to cover the fixed native `fee`, then restore the pool afterward — draining real value from other LPs in the pool while still meeting the chain's fee requirement.

### Finding Description
`withdraw_fee` in [1](#0-0)  computes the quantity of `asset_id` to withdraw as `S::quote_price_tokens_for_exact_tokens(asset_id, A::get(), fee, true)`. This quote is implemented in `pallet_asset_conversion::Pallet::quote_price_tokens_for_exact_tokens`, which reads the **live pool account balances** via `Self::get_reserves(...)` ( [2](#0-1)  and [3](#0-2) ) — i.e., raw on-chain balances of the pool account, not any time-weighted or depth-protected price.

The pallet's own documentation on this quote function explicitly warns: "Note that the price may have changed by the time the transaction is executed... (Use `amount_out_min`/`amount_in_max` to control slippage.)" — but `withdraw_fee` uses the quote as an *authoritative* system-determined value with no such bound, unlike normal user-initiated swaps which pass `amount_in_max`/`amount_out_min`.

The corrupted value is the pool's `(balance1, balance2)` reserve ratio returned by `get_reserves`, which any account can move by calling `add_liquidity`/`remove_liquidity` [4](#0-3) . Because these calls are ordinary, permissionless, unprivileged extrinsics with no bonding/lock-up delay (unlike OracleMaker's noted defense of "requires order being delayed"), an attacker who is also an LP of the `(asset_id, A)` pool can:

1. Submit `remove_liquidity` (or a skewed `add_liquidity`) to push the pool ratio so that `asset_id` looks artificially cheap relative to the native fee asset.
2. In the same block, submit the fee-paying extrinsic, letting `withdraw_fee` quote and immediately swap at this skewed ratio via `swap_tokens_for_exact_tokens` ( [5](#0-4) ), extracting exactly `fee` worth of native tokens from the pool for an artificially small amount of `asset_id`.
3. Restore the pool state afterward (e.g., re-add liquidity), pocketing the difference at the expense of the pool's other LPs, who absorb the resulting reserve imbalance.

No existing guard stops this: `ensure!(change.peek().is_zero())` only checks the swap consumed exactly the quoted input — it does not validate that the quote reflects a fair/undisturbed price. `ZeroLiquidity`/`PoolEmpty` checks only guard against literally empty pools, not against reserve skew.

### Impact Explanation
This falls under "public underpriced work that degrades block production" and "balances... conserve value and settle exactly once to the rightful beneficiary" from the Impact Gate: the fee-asset swap mechanism is a public entry point (any signed extrinsic using non-native fee payment) that lets an unprivileged LP/attacker extract value from other liquidity providers in a pool that the runtime treats as a fee-pricing oracle, with no economic safeguard analogous to OracleMaker's `minMarginRatio`. Repeated exploitation degrades the reliability of asset-based fee payment and directly steals value from LPs who did not consent to being counterparties in a manipulated trade.

### Likelihood Explanation
Any account can be both an LP and a fee-payer; `add_liquidity`/`remove_liquidity` are unprivileged, and pool reserve state persists across extrinsics within a block with no cooldown. This requires no validator, relayer, governance, or leaked-key assumption — only capital to move the pool ratio, exactly mirroring the OracleMaker "deposit → cheap action → withdraw" primitive, but executable atomically within a single block rather than needing an async withdrawal.

### Recommendation
Do not use unprotected instantaneous pool spot price for mandatory fee-asset conversion. Either: (a) require the extrinsic to specify a maximum `asset_id` amount (slippage bound) enforced against the quote, rejecting the transaction if the quoted price deviates unfavorably from a recent reference price; (b) use a time-weighted average price or minimum-liquidity-depth check before allowing the pool to be used as a `pallet-asset-conversion-tx-payment` fee-pricing source; or (c) rate-limit/lock liquidity changes relative to fee-quote usage in the same block, similar to the discussed OracleMaker mitigation of "implement a time lock when deposit/withdraw into makers."

### Proof of Concept
1. Attacker `A` seeds an `(asset_id, NativeToken)` pool via `AssetConversion::create_pool` + `add_liquidity`, becoming a majority LP.
2. In block `N`, `A` submits `AssetConversion::remove_liquidity` withdrawing most of the `NativeToken` side, skewing the ratio so `asset_id` is heavily overrepresented (cheap relative to native).
3. Still in block `N`, `A` submits a second extrinsic paying transaction fees in `asset_id` via `ChargeAssetTxPayment`/`SwapAssetAdapter`; `withdraw_fee` calls `quote_price_tokens_for_exact_tokens(asset_id, Native, fee, true)` against the now-skewed reserves, computing an artificially small `asset_fee`, then executes `swap_tokens_for_exact_tokens` which extracts the full native `fee` from the pool for that small `asset_fee` amount.
4. `A` submits `AssetConversion::add_liquidity` to restore the pool ratio, absorbing the arbitrage profit extracted from the pool's other LPs.
5. Repeat across blocks to systematically siphon value from the pool while every individual fee payment satisfies the chain's nominal fee requirement, masking the theft from the LPs bearing the loss.

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

**File:** substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs (L159-169)
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
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L1291-1316)
```rust
		/// Leading to an amount at the end of a `path`, get the required amounts in.
		pub(crate) fn balance_path_from_amount_out(
			amount_out: T::Balance,
			path: Vec<T::AssetKind>,
		) -> Result<BalancePath<T>, DispatchError> {
			let mut balance_path: BalancePath<T> = Vec::with_capacity(path.len());
			let mut amount_in: T::Balance = amount_out;

			let mut iter = path.into_iter().rev().peekable();
			while let Some(asset2) = iter.next() {
				let asset1 = match iter.peek() {
					Some(a) => a,
					None => {
						balance_path.push((asset2, amount_in));
						break;
					},
				};
				let fee = Self::pool_fee_for(asset1, &asset2)?;
				let (reserve_in, reserve_out) = Self::get_reserves(asset1.clone(), asset2.clone())?;
				balance_path.push((asset2, amount_in));
				amount_in = Self::get_amount_in(fee, &amount_in, &reserve_in, &reserve_out)?;
			}
			balance_path.reverse();

			Ok(balance_path)
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
