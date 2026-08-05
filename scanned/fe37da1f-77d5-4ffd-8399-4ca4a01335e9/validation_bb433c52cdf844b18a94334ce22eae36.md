Confirmed: `get_balance` in `substrate/frame/asset-conversion/src/lib.rs` is a live `T::Assets::balance(asset, owner)` lookup, not an internally-tracked reserve, and `do_add_liquidity`/`get_reserves` derive `reserve1`/`reserve2` directly from that call. This is a real local analog of the DODO "donation-inflates-ratio-without-minting" primitive.

### Title
First-LP Share-Ratio Manipulation via Direct Token Donation Causes `InsufficientLiquidityMinted` DOS in `pallet-asset-conversion` - (File: `substrate/frame/asset-conversion/src/lib.rs`)

### Summary
`pallet-asset-conversion` computes pool reserves as the *live* free balance of the pool's sovereign account rather than from LP-token-accounted state [1](#0-0) , and `do_add_liquidity` uses these live reserves together with `total_issuance` of the LP token to price subsequent deposits [2](#0-1) . An attacker can shrink LP-token `total_supply` to the protocol-minimum while permissionlessly inflating the pool account's raw token balances via ordinary transfers ("donation", the same primitive as the DODO `sync()` step), which distorts the balance-to-supply ratio and causes legitimate depositors' `add_liquidity` calls to revert.

### Finding Description
`do_add_liquidity` reads `reserve1`/`reserve2` via `Self::get_balance`, which is nothing more than `T::Assets::balance(asset, owner)` [1](#0-0) . Unlike an internally maintained reserve counter, this value can be changed by *anyone* simply transferring tokens to the pool's sovereign account — no `add_liquidity` call, no LP-token mint, is required.

When `total_supply` is non-zero, new LP tokens are minted as `min(amount1 * total_supply / reserve1, amount2 * total_supply / reserve2)` [3](#0-2) , and the call reverts unless the result exceeds `MintMinLiquidity` [4](#0-3) . Because `reserve1`/`reserve2` are attacker-inflatable while `total_supply` is not tied to those balances, an attacker can:

1. Create a pool and add minimal liquidity, receiving `lp_token_amount = sqrt(amount1*amount2) - MintMinLiquidity` while `MintMinLiquidity` (a fixed constant) is permanently locked in the pool account [5](#0-4) .
2. Call `remove_liquidity` to burn down their own LP tokens to near zero, shrinking `total_supply` to essentially `MintMinLiquidity` [6](#0-5) .
3. Directly transfer ("donate") a large amount of `asset1`/`asset2` to the pool's sovereign account (ordinary token transfer, no pallet call needed), which is picked up unconditionally as `reserve1`/`reserve2` on the next `get_balance` read.
4. Any subsequent user calling `add_liquidity` with an amount smaller than the new reserve-to-supply ratio gets `side1`/`side2` truncated toward zero by integer division, tripping `ensure!(lp_token_amount > T::MintMinLiquidity::get(), Error::<T>::InsufficientLiquidityMinted)` and reverting [4](#0-3) .

Existing guards do not stop this: `MintMinLiquidity` only bounds the *initial* mint against the well-known first-depositor share-inflation attack (mirroring the DODO recommendation of locking a minimum amount permanently), but it does nothing to prevent post-creation reserve inflation via raw transfer, because reserves are never decoupled from directly-controllable account balances. `ok_to_be_open`-style ratio caps exist for nomination-pools (`points_to_balance_ratio_floor < MaxPointsToBalance`, see `substrate/frame/nomination-pools/src/lib.rs:1189-1202`) but `pallet-asset-conversion` has no equivalent cap on the reserve/`total_supply` ratio for `add_liquidity`.

### Impact Explanation
This causes public underpriced/blocked work: legitimate liquidity providers depositing reasonable amounts are denied via `InsufficientLiquidityMinted`, degrading the DEX pool's ability to accept new TVL — directly matching the "public underpriced work that degrades block production or stalls bridge processing" and general chain-liveness impact categories for this program, applied here to a runtime pallet's core liquidity-provision entry point. The severity is bounded by the fact that the attacker's donation cost scales with `total_supply`, so — unlike the original 1001x-leverage DODO exploit — the attacker cannot force an arbitrarily large-vs-cost DoS; the leverage ratio here is capped near `MintMinLiquidity / total_supply_after_burn`, which is close to 1x once the attacker drains their own LP tokens. Still, it is a real, permissionless, no-privilege DoS on `add_liquidity` for a given pool, forcing victims to match the manipulated ratio or be reverted, and it can be repeated per pool by any user who bootstraps or re-manipulates the pool.

### Likelihood Explanation
High feasibility: `create_pool`/`add_liquidity`/`remove_liquidity` are all public, unprivileged extrinsics, and donating tokens directly to a known sovereign account requires only a standard balance/asset transfer — no governance, no validator, no relayer involvement. The pool's sovereign account address is deterministically derivable from `PoolLocator`, so any attacker can target any pool. The main constraint is economic (the attacker must front the donation), which the Sherlock discussion itself rated as only a Medium precisely because of this same capital-cost caveat.

### Recommendation
Track pool reserves in dedicated pallet storage (updated atomically on `add_liquidity`/`remove_liquidity`/`swap`) instead of deriving them from a live, freely-transferable account balance, or clamp `reserve` used in pricing to the accounted deposits only (e.g., ignore balances above what was ever contributed via tracked LP operations). Alternatively, apply a `sync()`-style explicit reconciliation only under permissioned or rate-limited conditions, and add a ratio-drift cap analogous to nomination-pools' `MaxPointsToBalance` check so an outsized reserve/`total_supply` ratio triggers pool-state protection rather than reverting depositors.

### Proof of Concept
Conceptual sequence (Substrate/asset-conversion pallet, no privileged calls):
1. `create_pool(asset1, asset2)` then `add_liquidity(asset1, asset2, 10_000, 10_000, 1, 1, attacker)` → mints `MintMinLiquidity` to pool account, `~9_900` to attacker (see `do_add_liquidity`, lines 858-877 above).
2. `remove_liquidity(asset1, asset2, 9_899, 0, 0, attacker)` → burns almost all attacker LP, leaving `total_supply ≈ MintMinLiquidity + 1`.
3. Attacker sends an ordinary `Assets::transfer`/`Balances::transfer` of a large amount of `asset1` and `asset2` directly to the pool's sovereign account (derived via `PoolLocator::pool_address`).
4. Victim calls `add_liquidity(asset1, asset2, victim_amount1, victim_amount2, ..., victim)` with a "normal" deposit amount; `side1`/`side2` computed from the now-inflated `reserve1`/`reserve2` truncate to ≤ `MintMinLiquidity`, and the call reverts with `Error::InsufficientLiquidityMinted` (lines 858-877), denying the victim's liquidity provision until they match the manipulated ratio.

### Citations

**File:** substrate/frame/asset-conversion/src/lib.rs (L813-814)
```rust
			let reserve1 = Self::get_balance(&pool_account, asset1.clone());
			let reserve2 = Self::get_balance(&pool_account, asset2.clone());
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L858-872)
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
			} else {
				let side1 = Self::mul_div(&amount1, &total_supply, &reserve1)?;
				let side2 = Self::mul_div(&amount2, &total_supply, &reserve2)?;
				lp_token_amount = side1.min(side2);
			}
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L874-877)
```rust
			ensure!(
				lp_token_amount > T::MintMinLiquidity::get(),
				Error::<T>::InsufficientLiquidityMinted
			);
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L894-920)
```rust
		/// Remove liquidity from a pool.
		pub(crate) fn do_remove_liquidity(
			who: &T::AccountId,
			asset1: T::AssetKind,
			asset2: T::AssetKind,
			lp_token_burn: T::Balance,
			amount1_min_receive: T::Balance,
			amount2_min_receive: T::Balance,
			withdraw_to: &T::AccountId,
		) -> Result<(T::Balance, T::Balance), DispatchError> {
			let pool_id = T::PoolLocator::pool_id(&asset1, &asset2)
				.map_err(|_| Error::<T>::InvalidAssetPair)?;

			ensure!(lp_token_burn > Zero::zero(), Error::<T>::ZeroLiquidity);

			let pool = Pools::<T>::get(&pool_id).ok_or(Error::<T>::PoolNotFound)?;

			let pool_account =
				T::PoolLocator::address(&pool_id).map_err(|_| Error::<T>::InvalidAssetPair)?;
			let (reserve1, reserve2) = Self::get_reserves(asset1.clone(), asset2.clone())?;

			let total_supply = T::PoolAssets::total_issuance(pool.lp_token.clone());
			let withdrawal_fee_amount = T::LiquidityWithdrawalFee::get() * lp_token_burn;
			let lp_redeem_amount = lp_token_burn.saturating_sub(withdrawal_fee_amount);

			let amount1 = Self::mul_div(&lp_redeem_amount, &reserve1, &total_supply)?;
			let amount2 = Self::mul_div(&lp_redeem_amount, &reserve2, &total_supply)?;
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L1265-1269)
```rust
		/// Get the `owner`'s balance of `asset`, which could be the chain's native asset or another
		/// fungible. Returns a value in the form of an `Balance`.
		pub(crate) fn get_balance(owner: &T::AccountId, asset: T::AssetKind) -> T::Balance {
			T::Assets::balance(asset, owner)
		}
```
