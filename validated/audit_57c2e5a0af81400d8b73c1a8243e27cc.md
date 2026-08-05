Audit Report

## Title
Reserve inflation via direct donation to the deterministic pool account lets an unprivileged user dilute later liquidity providers in `pallet-asset-conversion` - (`substrate/frame/asset-conversion/src/lib.rs`)

## Summary
`do_add_liquidity` derives the LP-mint ratio from the pool account's live, full asset balance (`reserve1`/`reserve2`) rather than from an internally tracked reserve, and the pool's sovereign account is a deterministically derivable `PalletId`-based address. An unprivileged attacker can transfer tokens directly to that account outside of `pallet-asset-conversion`, inflating the reserves used in the `total_supply`-based mint ratio in `do_add_liquidity`, so a subsequent depositor's minted LP share (`lp_token_amount = side1.min(side2)`) is diluted well below their proportional contribution while their real tokens are transferred into the pool.

## Finding Description
`do_add_liquidity` reads `reserve1`/`reserve2` as raw account balances via `Self::get_balance(&pool_account, ...)` [1](#0-0) , and once `total_supply` is non-zero, computes the LP mint amount purely from that manipulable ratio: `side1 = amount1*total_supply/reserve1`, `side2 = amount2*total_supply/reserve2`, `lp_token_amount = side1.min(side2)` [2](#0-1) . Because `pool_account` is derived via `T::PoolLocator::address(&pool_id)` — a deterministic, publicly computable address — any account can inflate `reserve1`/`reserve2` by sending tokens directly to that address via ordinary `pallet_assets::transfer` or `pallet_balances::transfer_allow_death`, entirely bypassing `add_liquidity`. This raises the denominator in `side1`/`side2` without increasing `total_supply`, so a legitimate depositor's minted LP tokens are diluted relative to the value of tokens they transfer in at lines 855-856. The only protective check, `ensure!(lp_token_amount > T::MintMinLiquidity::get(), ...)` [3](#0-2) , only guards against a near-zero mint, not against a heavily diluted but nonzero mint, and `MintMinLiquidity` also only applies to the initial (`total_supply.is_zero()`) mint branch. The `add_liquidity` extrinsic does not expose any `min_lp_token_amount`-style parameter to bound the minted share directly — the only slippage protections (`amount1_min`, `amount2_min`) bound the *deposited* token amounts, not the LP tokens received, so they do not protect against this dilution.

## Impact Explanation
This is a real value-transfer bug: an attacker who holds (or controls) most of a pool's existing LP supply can donate tokens directly to the pool account to inflate reserves, causing a subsequent depositor's real tokens to be absorbed into the pool while they receive a disproportionately small LP-token claim. Because `total_supply` grows only by the diluted `lp_token_amount`, the attacker's pre-existing LP share captures a share of the victim's newly deposited value once withdrawn via `remove_liquidity`, and the attacker can later reclaim their own donated tokens back through their still-dominant LP share. This matches "theft or unbacked mint" / wrong-beneficiary-amount impact classes, executed entirely through public extrinsics/transfers with no privileged role.

## Likelihood Explanation
Exploitability requires a pool with thin liquidity (low `total_supply`) where the attacker holds a large fraction of existing LP tokens — the same precondition documented in the report's own PoC (pool creator adds minimal liquidity just above `MintMinLiquidity`). The pool address is computable off-chain by any party without privileged access, and the donation is a standard `transfer`. This is a realistic, low-cost, repeatable griefing/theft vector against freshly created or thinly liquid pools, though it is less effective against deep pools where the attacker's own LP share is small.

## Recommendation
Track pool reserves in an internal, mint-controlled storage value (e.g., within `PoolInfo`) updated only by `do_add_liquidity`/`do_remove_liquidity`/swap logic, instead of reading the pool account's live balance for LP-mint-ratio math. Alternatively, expose a `min_lp_token_amount` parameter on `add_liquidity` so depositors can bound the minimum LP tokens they are willing to accept, providing slippage protection against reserve manipulation between quote and execution.

## Proof of Concept
1. User A creates pool `(asset1, asset2)` and adds minimal liquidity via `add_liquidity`, receiving `total_supply` slightly above `MintMinLiquidity`.
2. Attacker computes the pool's sovereign account via `T::PoolLocator::address` (public, deterministic) and sends a large amount of `asset1`/`asset2` directly to that account via `pallet_assets::transfer`/`pallet_balances::transfer_allow_death`, bypassing `pallet-asset-conversion` entirely.
3. Victim calls `add_liquidity` with a reasonable, price-quoted amount; `do_add_liquidity` transfers the victim's tokens into the now-inflated pool account (lines 855-856) and mints `lp_token_amount = side1.min(side2)` computed off the inflated `reserve1`/`reserve2` (lines 868-872), giving the victim a tiny LP share relative to their deposit.
4. Attacker (or User A, who controls most of `total_supply`) calls `remove_liquidity` to redeem a disproportionately large share of the pool's real assets, extracting the victim's deposited value and recovering their own donated tokens.

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
