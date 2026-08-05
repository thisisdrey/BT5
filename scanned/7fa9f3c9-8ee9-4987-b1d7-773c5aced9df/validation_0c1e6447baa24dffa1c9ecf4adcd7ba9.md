## Title
`do_remove_liquidity` computes withdrawable reserves from full pool-account balance instead of the actually reducible balance, letting LP-token holders be locked out of withdrawals - ([File: substrate/frame/asset-conversion/src/lib.rs])

### Summary
`pallet-asset-conversion` computes pool reserves via `Self::get_balance`, which reads the *full* account balance (`T::Assets::balance`) rather than the *reducible* balance. This full-balance figure is used both to price liquidity provision (`do_add_liquidity`) and to compute the exact amounts owed on `remove_liquidity` (`do_remove_liquidity`). The only place in the pallet that additionally checks the pool account's actual withdrawable ("reducible") balance before promising an amount is the *view* functions `quote_price_exact_tokens_for_tokens` / `quote_price_tokens_for_exact_tokens` — the real state-changing extrinsic `remove_liquidity` has no equivalent ceiling check. This mirrors the Panoptic bug class: "available assets" bookkeeping (full reserve) does not account for the portion of the pool account's balance that is not actually free to move (held/frozen amounts, or ED lock on the paired asset), so LP withdrawals can be computed as satisfiable when they are not, and fail or partially strand liquidity when actually attempted.

### Finding Description
- `do_add_liquidity` and `do_remove_liquidity` both derive reserves through `get_balance`/`get_reserves`, which is simply `T::Assets::balance(asset, owner)` — the full ledger balance: [1](#0-0) [2](#0-1) 
- `do_remove_liquidity` uses this full-balance `get_reserves` result to compute `amount1`/`amount2` owed to the withdrawer, and then unconditionally attempts `T::Assets::transfer(..., Expendable)` for that exact amount: [3](#0-2) 
- By contrast, the *quote* (view, non-state-changing) functions explicitly acknowledge that full balance can overstate what is actually transferable, and clamp the output to `T::Assets::reducible_balance(..., Preserve, Polite)` before returning a quote: [4](#0-3) [5](#0-4) 
- This asymmetry is confirmed by the pallet's own PR history: `pr_12408.prdoc` shows the pallet was deliberately switched from reducible to full balances for "pool prices and liquidity amounts" specifically because reducible balance "could understate pool reserves when protected funds or unrelated non-sufficient assets were held in the pool account": [6](#0-5) 

The corrupted value is the reserve figure (`reserve1`/`reserve2` in `do_remove_liquidity`, and equivalently in `do_add_liquidity`) used to compute LP pricing and withdrawal amounts. It represents the *total* ledger balance of the pool account rather than the balance that is actually free to leave that account (i.e., balance minus ED-lock, holds, or freezes on the asset). Existing guards do not stop this: the `ReserveLeftLessThanMinimal` checks in `do_remove_liquidity` only verify that the *computed post-withdrawal reserve* (still derived from the full-balance figure) stays above `T::Assets::minimum_balance`, not that the *pre-withdrawal amount to transfer* is actually reducible. There is no call to `reducible_balance` anywhere in `do_add_liquidity`/`do_remove_liquidity`, unlike the parallel quote functions that were patched to add exactly this check.

### Impact Explanation
If a pool account ever holds an amount of an asset that is not fully reducible (e.g. the asset's existential deposit locks the last units, or a hold/freeze exists on the asset for the pool account, or successive multi-hop swaps in `credit_swap`/`swap` leave transient balances under `Preserve` semantics), then:
1. LP-token pricing in `do_add_liquidity` will be computed against an inflated reserve figure, mispricing LP token issuance relative to what liquidity providers could actually redeem.
2. LP-token holders in `do_remove_liquidity` can have their entitled withdrawal amount computed larger than what the pool account can actually transfer, causing the `T::Assets::transfer(..., Expendable)` call to fail with a runtime error, permanently blocking that withdrawal until the pool's real balance catches up — a direct analog to Panoptic's "long positions disabled from closing until further liquidity provision." LP funds become effectively locked in the pool, unable to be redeemed even though pool state formally shows sufficient LP-token backing.

This falls within the "public underpriced work / permanent user-fund lock" impact category for asset/pool accounting per the program's impact gate, since it is reachable by any unprivileged account calling the public `add_liquidity`/`remove_liquidity`/`swap_*` extrinsics with no admin, governance, or malicious-peer precondition.

### Likelihood Explanation
Likelihood is moderate. It requires the pool account balance to diverge between "full" and "reducible" — via ED constraints on the paired asset, a `Freezer`/`Holder` implementation attaching holds/freezes to the pool account, or transient states created during multi-hop swap paths (`credit_swap` moves intermediate credits with `Preserve` across pool accounts in a chain). Because the pallet is generic over `T::Assets`/`T::PoolAssets` and used across multiple runtimes (Asset Hub, precompiles, XCM trader), the concrete conditions vary by deployment, but the code path itself has no defensive check preventing the divergence from causing a failed/blocked withdrawal.

### Recommendation
Apply the same defensive clamp used in `quote_price_exact_tokens_for_tokens`/`quote_price_tokens_for_exact_tokens` to the state-changing paths: in `do_remove_liquidity` (and ideally `do_add_liquidity`'s reserve computation), bound the computed transfer amount by `T::Assets::reducible_balance(asset, &pool_account, Expendable/Preserve, Polite)` before committing to it, or otherwise fail early with a clear error (rather than a raw transfer failure) whenever full-balance-derived entitlements exceed what is actually reducible. Ensure LP pricing and payout computations are consistently based on the same "actually reducible" reserve figure across add/remove/swap/quote code paths.

### Proof of Concept
1. Configure a pool where the paired asset's `T::Assets` implementation places a non-zero, non-withdrawable amount on the pool account (e.g. via a `Freezer`/`Holder` extension, or via ED-driven `minimum_balance` retention combined with a near-zero remaining balance after a series of swaps).
2. Call `add_liquidity` — LP token minted amount is computed from `get_balance` (full balance), inflating `reserve1`/`reserve2` beyond what is truly redeemable.
3. Later, an LP calls `remove_liquidity` with `lp_token_burn` proportional to their share; `do_remove_liquidity` computes `amount1`/`amount2` from the same inflated `get_reserves` figure.
4. The subsequent `T::Assets::transfer(asset1, &pool_account, withdraw_to, amount1, Expendable)` fails because the pool account's actually reducible balance is less than `amount1`, reproducing the “insufficient liquidity to honor the accounted entitlement” condition — the local analog of Panoptic's disabled long-position exercise.

### Citations

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

**File:** substrate/frame/asset-conversion/src/lib.rs (L1549-1562)
```rust
			// Small inputs can round output to zero due to integer division.
			if amount_out.is_zero() {
				return None;
			}

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

**File:** prdoc/pr_12408.prdoc (L1-11)
```text
title: 'fix(asset-conversion): use full balances for pool prices'
doc:
- audience: Runtime Dev
  description: |
    `pallet-asset-conversion` now reads full pool account balances when calculating
    pool prices and liquidity amounts. Previously, these calculations used reducible
    balances, which could understate pool reserves when protected funds or unrelated
    non-sufficient assets were held in the pool account.
crates:
- name: pallet-asset-conversion
  bump: patch
```
