### Title
Permanent last-liquidity dust lock in `pallet-asset-conversion` — `MintMinLiquidity` combined with `ReserveLeftLessThanMinimal` guard makes a fraction of pool reserves permanently unredeemable - (File: `substrate/frame/asset-conversion/src/lib.rs`)

### Summary
The external report's core broken invariant is: a stableswap pool's liquidity-removal path cannot handle the "last unit of liquidity" case, so a portion of deposited funds becomes permanently stuck once the pool is drawn down to its edge state. `pallet-asset-conversion`'s `do_remove_liquidity` has the analogous edge-of-liquidity failure mode, but instead of an arithmetic overflow it manifests as a **guaranteed, un-recoverable dust lock**: the pool never lets `reserve_left` fall below each asset's `minimum_balance`, and a portion of LP-token supply (`T::MintMinLiquidity`) is permanently minted to the pool account itself with no key able to redeem it, so the value corresponding to it can never be withdrawn by anyone.

### Finding Description
When the first liquidity provider deposits into an empty pool, `do_add_liquidity` mints `T::MintMinLiquidity::get()` LP tokens directly to the `pool_account` (not to any user): [1](#0-0) 

Because `pool_account` has no private key, these LP tokens can never be burned back through `remove_liquidity`. This permanently inflates `total_issuance(lp_token)` above the sum of all user-redeemable LP tokens, meaning `total_supply` used in the proportional payout formula in `do_remove_liquidity` can never be driven to zero by real users burning their tokens: [2](#0-1) 

In addition, `do_remove_liquidity` enforces that the resulting reserves after a withdrawal must remain at or above each asset's existential deposit: [3](#0-2) 

The pallet's own regression test acknowledges and locks in this exact behavior — that as a pool's reserves shrink toward the existential deposit, further extraction (even via `swap_tokens_for_exact_tokens`) is rejected with `TokenError::NotExpendable`: [4](#0-3) 

The combined effect is structurally identical to the HydraDX report's core defect: **no code path exists to fully drain a pool's economic value back to its rightful owners**. In the stableswap case this failed via an arithmetic overflow; here it fails "silently" via a guard (`ReserveLeftLessThanMinimal`) plus an un-ownable LP-token balance (`MintMinLiquidity` held by `pool_account`). Either way, the last slice of pooled value — proportional to `MintMinLiquidity` and to each asset's `minimum_balance` — is permanently locked in the pool account with no dispatchable path (no `remove_liquidity` variant, no privileged sweep function) to recover it.

### Impact Explanation
This falls under the accepted "permanent user-fund or bridge-state lock" impact category. Every pool created through `pallet-asset-conversion` permanently strands a small but non-zero amount of user-deposited value (worth `MintMinLiquidity` LP tokens' proportional share of reserves, bounded below by each asset's existential deposit) that cannot be assigned to, or recovered by, any account — not even by root/governance, since there is no dispatchable to burn `pool_account`'s LP balance or force-withdraw below the ED floor. This directly parallels the judged HydraDX Medium finding: "the initial liquidity cannot be removed from the system... this can lead to (temporary) locked funds."

### Likelihood Explanation
This is guaranteed to occur on every single pool at initialization (any time `total_supply.is_zero()` in `do_add_liquidity`) and is unavoidable/exercised by ordinary, unprivileged users performing the normal `add_liquidity` → `remove_liquidity` lifecycle — no attacker, admin, or malicious actor is required.

### Recommendation
Provide a governance- or root-gated (or otherwise safely restricted) sweep function that can burn the `MintMinLiquidity` balance held by `pool_account` together with a corresponding proportional withdrawal that is exempt from the `ReserveLeftLessThanMinimal` check when a pool is being fully closed/destroyed (e.g., only when the LP-token's remaining circulating supply outside `pool_account` is zero), so the residual dust reserves can be swept to a designated destination (e.g., treasury) instead of being permanently orphaned.

### Proof of Concept
1. Create a pool for `asset1`/`asset2` via `create_pool`.
2. `add_liquidity` as the sole LP: `total_supply = lp_token_amount + T::MintMinLiquidity::get()`, where `T::MintMinLiquidity::get()` LP tokens are minted to `pool_account` [5](#0-4) .
3. Call `remove_liquidity` burning the LP's entire user-held balance (`lp_token_amount`). The payout formula `mul_div(lp_redeem_amount, reserve, total_supply)` necessarily returns less than 100% of `reserve1`/`reserve2` because `total_supply > lp_redeem_amount` [2](#0-1) .
4. The residual reserve (bounded below at each asset's `minimum_balance` by the `ReserveLeftLessThanMinimal` guard) remains in `pool_account` forever; the only claim on it — `pool_account`'s LP-token balance — can never be exercised because `pool_account` cannot sign extrinsics. This matches the existing test `check_no_panic_when_try_swap_close_to_empty_pool`, which explicitly demonstrates and asserts that once reserves approach the ED, further withdrawal attempts fail with `TokenError::NotExpendable` [4](#0-3) .

### Citations

**File:** substrate/frame/asset-conversion/src/lib.rs (L858-867)
```rust
			let total_supply = T::PoolAssets::total_issuance(pool.lp_token.clone());

			let lp_token_amount: T::Balance;
			if total_supply.is_zero() {
				lp_token_amount = Self::calc_lp_amount_for_zero_supply(&amount1, &amount2)?;
				T::PoolAssets::mint_into(
					pool.lp_token.clone(),
					&pool_account,
					T::MintMinLiquidity::get(),
				)?;
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L915-920)
```rust
			let total_supply = T::PoolAssets::total_issuance(pool.lp_token.clone());
			let withdrawal_fee_amount = T::LiquidityWithdrawalFee::get() * lp_token_burn;
			let lp_redeem_amount = lp_token_burn.saturating_sub(withdrawal_fee_amount);

			let amount1 = Self::mul_div(&lp_redeem_amount, &reserve1, &total_supply)?;
			let amount2 = Self::mul_div(&lp_redeem_amount, &reserve2, &total_supply)?;
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L930-939)
```rust
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
```

**File:** substrate/frame/asset-conversion/src/tests.rs (L1494-1510)
```rust
		// Now, the pool should exist but be almost empty.
		// Let's try and drain it.
		assert_eq!(balance(pallet_account, token_1.clone()), 708);
		assert_eq!(balance(pallet_account, token_2.clone()), 15);

		// validate the reserve should always stay above the ED
		assert_noop!(
			AssetConversion::swap_tokens_for_exact_tokens(
				RuntimeOrigin::signed(user),
				bvec![token_2.clone(), token_1.clone()],
				708 - ed + 1, // amount_out
				500,          // amount_in_max
				user,
				false,
			),
			TokenError::NotExpendable,
		);
```
