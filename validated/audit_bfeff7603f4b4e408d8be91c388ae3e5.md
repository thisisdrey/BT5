Audit Report

## Title
`do_remove_liquidity` computes withdrawable reserves from full pool-account balance instead of the actually reducible balance, letting LP-token holders be locked out of withdrawals - ([File: substrate/frame/asset-conversion/src/lib.rs])

## Summary
`pallet-asset-conversion`'s `get_balance`/`get_reserves` read the full ledger balance (`T::Assets::balance`) of the pool account rather than the actually transferable ("reducible") balance, and `do_remove_liquidity` uses this full-balance figure to compute `amount1`/`amount2` owed to an LP withdrawer before unconditionally calling `T::Assets::transfer(..., Expendable)`. The pallet's own `quote_price_exact_tokens_for_tokens`/`quote_price_tokens_for_exact_tokens` view functions explicitly clamp their output against `T::Assets::reducible_balance(..., Preserve, Polite)` precisely because full balance can overstate what is transferable, but this same defensive check is absent from the state-changing `do_remove_liquidity`/`do_add_liquidity` paths.

## Finding Description
`get_balance` at [1](#0-0)  simply returns `T::Assets::balance(asset, owner)` — the full ledger balance, with no consideration of holds, freezes, or ED-locked amounts. `get_reserves` at [2](#0-1)  builds `(balance1, balance2)` directly from this full-balance `get_balance`.

`do_remove_liquidity` uses this full-balance-derived `(reserve1, reserve2)` to compute the exact `amount1`/`amount2` owed to the withdrawer via `mul_div`, checks only that the *post-withdrawal reserve* (still full-balance-derived) stays above `T::Assets::minimum_balance`, and then unconditionally calls `T::Assets::transfer(asset1, &pool_account, withdraw_to, amount1, Expendable)` and the equivalent for `asset2`: [3](#0-2) . There is no call to `reducible_balance` anywhere in this function to verify that `amount1`/`amount2` are actually obtainable from the pool account before committing to burning LP tokens and attempting the transfer.

By contrast, the parallel *view* functions `quote_price_exact_tokens_for_tokens` and `quote_price_tokens_for_exact_tokens` explicitly compute `max_output = T::Assets::reducible_balance(asset2, &pool_account, Preserve, Polite)` and refuse to return a quote larger than that ceiling: [4](#0-3)  and [5](#0-4) . This confirms the pallet authors recognized that full balance can diverge from the truly withdrawable amount, but the fix was applied only to the read-only quoting paths, not to the state-changing `do_remove_liquidity` (or `do_add_liquidity`'s reserve computation at [6](#0-5) ) that actually moves and burns LP tokens/funds.

The `ReserveLeftLessThanMinimal` guards in `do_remove_liquidity` only bound the *residual* reserve against `T::Assets::minimum_balance`, which protects the ED but does not detect or prevent divergence caused by holds/freezes placed on the pool account by `T::Assets`'s `fungibles::Mutate`/`Hold`/`Freeze` implementations — these guards are insufficient against the exact failure mode described.

## Impact Explanation
When a pool account's full balance exceeds its reducible balance (e.g., due to a hold/freeze from a `T::Assets` implementation, or transient states from multi-hop swap paths), `do_add_liquidity` mints LP tokens against an inflated reserve figure, and `do_remove_liquidity` computes a withdrawal amount larger than what can actually leave the pool account. The LP-token burn (`T::PoolAssets::burn_from`) happens before the transfer attempt, so a failing `T::Assets::transfer(..., Expendable)` call reverts the whole extrinsic (via `DispatchError` propagation), permanently blocking that LP's ability to redeem their share until the pool's actual reducible balance recovers — a public, unprivileged, permanent lock of user funds reachable via the public `remove_liquidity`/`add_liquidity` extrinsics, matching the "permanent user-fund ... lock" category in the impact gate.

## Likelihood Explanation
Likelihood depends on the concrete `T::Assets` implementation used by a given runtime placing holds/freezes on the pool account, or on ED-driven divergence from multi-asset/multi-hop configurations. Since `pallet-asset-conversion` is generic over `T::Assets` and deployed in multiple runtimes (e.g., Asset Hub), this condition is plausible but requires a `Freezer`/`Holder`-capable asset implementation acting on the pool account — reachable purely through public extrinsics with no privileged, governance, or malicious-peer precondition.

## Recommendation
Apply the same `reducible_balance(..., Preserve/Expendable, Polite)` clamp used in the quote functions to `do_remove_liquidity` (and to `do_add_liquidity`'s reserve computation) before computing `amount1`/`amount2`, or fail early with a dedicated error when the full-balance-derived entitlement exceeds the actually reducible amount, rather than letting a raw `transfer` failure abort the extrinsic after the LP-token burn has already been validated.

## Proof of Concept
1. Configure a pool where `T::Assets` places a non-zero, non-withdrawable hold/freeze on the pool account for one of the pooled assets.
2. Call `add_liquidity`; LP token minted amount is computed from `get_balance` (full balance), inflating `reserve1`/`reserve2`.
3. An LP calls `remove_liquidity` with `lp_token_burn` proportional to their share; `do_remove_liquidity` computes `amount1`/`amount2` from the same inflated `get_reserves` figure, burns the LP tokens, then calls `T::Assets::transfer(asset1, &pool_account, withdraw_to, amount1, Expendable)`.
4. The transfer fails because the pool account's actually reducible balance is less than `amount1`, reproducing the permanent-lock condition for that LP's redeemable share.

### Citations

**File:** substrate/frame/asset-conversion/src/lib.rs (L813-814)
```rust
			let reserve1 = Self::get_balance(&pool_account, asset1.clone());
			let reserve2 = Self::get_balance(&pool_account, asset2.clone());
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L911-952)
```rust
			let pool_account =
				T::PoolLocator::address(&pool_id).map_err(|_| Error::<T>::InvalidAssetPair)?;
			let (reserve1, reserve2) = Self::get_reserves(asset1.clone(), asset2.clone())?;

			let total_supply = T::PoolAssets::total_issuance(pool.lp_token.clone());
			let withdrawal_fee_amount = T::LiquidityWithdrawalFee::get() * lp_token_burn;
			let lp_redeem_amount = lp_token_burn.saturating_sub(withdrawal_fee_amount);

			let amount1 = Self::mul_div(&lp_redeem_amount, &reserve1, &total_supply)?;
			let amount2 = Self::mul_div(&lp_redeem_amount, &reserve2, &total_supply)?;

			ensure!(
				!amount1.is_zero() && amount1 >= amount1_min_receive,
				Error::<T>::AssetOneWithdrawalDidNotMeetMinimum
			);
			ensure!(
				!amount2.is_zero() && amount2 >= amount2_min_receive,
				Error::<T>::AssetTwoWithdrawalDidNotMeetMinimum
			);
			let reserve1_left = reserve1.saturating_sub(amount1);
			let reserve2_left = reserve2.saturating_sub(amount2);
			ensure!(
				reserve1_left >= T::Assets::minimum_balance(asset1.clone()),
				Error::<T>::ReserveLeftLessThanMinimal
			);
			ensure!(
				reserve2_left >= T::Assets::minimum_balance(asset2.clone()),
				Error::<T>::ReserveLeftLessThanMinimal
			);

			// burn the provided lp token amount that includes the fee
			T::PoolAssets::burn_from(
				pool.lp_token.clone(),
				who,
				lp_token_burn,
				Expendable,
				Exact,
				Polite,
			)?;

			T::Assets::transfer(asset1, &pool_account, withdraw_to, amount1, Expendable)?;
			T::Assets::transfer(asset2, &pool_account, withdraw_to, amount2, Expendable)?;
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L1265-1269)
```rust
		/// Get the `owner`'s balance of `asset`, which could be the chain's native asset or another
		/// fungible. Returns a value in the form of an `Balance`.
		pub(crate) fn get_balance(owner: &T::AccountId, asset: T::AssetKind) -> T::Balance {
			T::Assets::balance(asset, owner)
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

**File:** substrate/frame/asset-conversion/src/lib.rs (L1554-1562)
```rust
			// Swap withdrawals from pools use `keep_alive=true` (Preserve). Use the same
			// preservation level to determine the actual withdrawable amount.
			let max_output = T::Assets::reducible_balance(asset2, &pool_account, Preserve, Polite);
			if amount_out > max_output {
				return None;
			}

			Some(amount_out)
		}
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L1589-1596)
```rust
			// Swap withdrawals from pools use `keep_alive=true` (Preserve). Use the same
			// preservation level to determine the actual withdrawable amount.
			let max_output =
				T::Assets::reducible_balance(asset2.clone(), &pool_account, Preserve, Polite);
			if amount > max_output {
				return None;
			}

```
