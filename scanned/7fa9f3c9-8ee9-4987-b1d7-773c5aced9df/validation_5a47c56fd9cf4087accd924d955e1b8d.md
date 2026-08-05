### Title
Asset-conversion LP pools can be driven back into a low-supply, donation-attack-vulnerable state after initial liquidity is mostly withdrawn - (File: `substrate/frame/asset-conversion/src/lib.rs`)

### Summary
`pallet-asset-conversion` mints LP shares proportional to a ratio of deposited assets to the pool's raw token balances (reserves), the same accounting pattern flagged in the external ERC-4626 "donation attack" report. The pallet's only anti-inflation defense is a one-time `MintMinLiquidity` amount locked to the pool account at first mint. Unlike the vault's minimum *supply* invariant, this defense only guarantees `total_issuance > 0`, not that reserves/supply stay above an economically meaningful threshold. Because `do_remove_liquidity` has no floor tied to `MintMinLiquidity`/total supply (only tiny per-asset existential-deposit checks), any liquidity provider can add and then withdraw nearly all liquidity, leaving the pool back in a "low supply" state exactly like the one the report warns about — where a subsequent direct token donation to the pool account cheaply manipulates the reserve ratio and causes unfavorable rounding for the next depositor.

### Finding Description
LP token minting logic: [1](#0-0) 

- On first mint (`total_supply.is_zero()`), `MintMinLiquidity` is minted to the pool account itself (unspendable by any signed caller), and the depositor gets `calc_lp_amount_for_zero_supply(amount1, amount2)`, which subtracts `MintMinLiquidity` from `sqrt(amount1*amount2)`: [2](#0-1) 
- On subsequent mints, shares are `min(amount1*total_supply/reserve1, amount2*total_supply/reserve2)`, i.e. the same ratio-of-reserves calculation the external report identifies as manipulable via donation (direct token transfer to the pool account, since `reserve1`/`reserve2` are just `Self::get_balance(&pool_account, ...)` — a raw balance query, not an internally-tracked accounting value): [3](#0-2) 

Removal path has no protection tying withdrawal to keeping supply/reserves above a safe threshold — only per-asset existential-deposit checks: [4](#0-3) 

`MintMinLiquidity` (locked forever in the pool account) only prevents `total_supply` from reaching exactly zero (avoiding div-by-zero / permanent pool death); it is a small fixed constant and does **not** enforce that real, economically meaningful liquidity remains in the pool. Once a depositor withdraws almost all of their liquidity (leaving supply at `MintMinLiquidity + 1`), the pool is back in the same fragile state the vault report describes as "the same user could withdraw far below the minimum initial deposit amount... and the vault would be back in a vulnerable state" — except here it is the AMM pool, and the "donation" is a plain token `transfer` into the pool account (any account can call the underlying asset pallet's `transfer` to the pool address, since `T::Assets` is a standard fungibles interface, and the pool account is a deterministic, publicly known address from `T::PoolLocator::address`).

### Impact Explanation
An attacker who is (or colludes with) the dominant LP of a thin pool can:
1. Bootstrap the pool with real liquidity, then withdraw down to just above `MintMinLiquidity` via `remove_liquidity` (unprivileged, permissionless).
2. Donate a large amount of `asset1` directly to the pool account (plain `transfer`, no special permission).
3. Wait for/front a victim's `add_liquidity` call: the victim's `mul_div(amount, total_supply, reserve)` rounds down severely because `total_supply` is tiny relative to the inflated `reserve1`, so the victim receives disproportionately few (or, if below `MintMinLiquidity`, a rejected/failed) LP tokens for their deposited assets.
4. Attacker then withdraws proportional to their locked LP tokens after removing the donation effect, capturing the victim's contributed assets.

This is a public, non-privileged theft-of-value / mis-priced-share vulnerability against liquidity-provider funds, matching the "theft or unbacked mint" and "public underpriced work" impact categories in the gate.

### Likelihood Explanation
Moderate-to-high: it requires no validator, collator, governance, or leaked-key assumptions — only ordinary signed extrinsics (`remove_liquidity`, an asset `transfer`, and waiting for a normal user's `add_liquidity`). The pool account address is deterministically derivable from `T::PoolLocator`, so donations can be precisely targeted. The main constraint is finding thin, low-TVL pools (which is common for newly created or niche asset pairs on Asset Hub), and capital cost scales with how much reserve the attacker needs to distort — cheapest exactly when supply/reserves are near their post-withdrawal minimum, which is the state this design allows to recur indefinitely.

### Recommendation
Enforce a persistent minimum-reserve/minimum-supply invariant beyond the pool's lifetime, not just at first mint: e.g., disallow `remove_liquidity` from reducing `total_issuance(lp_token)` (or the reserves) below a configurable safety threshold tied to `MintMinLiquidity` scaled to real value, or require use of TWAP/last-block-cached reserves rather than spot balances for share-minting math so that a same-block/same-extrinsic donation cannot be leveraged before the next depositor's mint is computed.

### Proof of Concept
1. Attacker calls `create_pool` then `add_liquidity(asset1, asset2, X, X, ...)`, receiving `sqrt(X*X) - MintMinLiquidity` LP tokens; `MintMinLiquidity` LP tokens are locked to the pool account.
2. Attacker calls `remove_liquidity` to withdraw nearly all of `X`, leaving `total_issuance(lp_token) == MintMinLiquidity + ε` and reserves near the per-asset existential deposit.
3. Attacker (or a colluding account) calls `T::Assets::transfer` to send a large donation `D` of `asset1` directly to the deterministic pool account, inflating `reserve1` while `total_issuance` stays at `MintMinLiquidity + ε`.
4. Victim calls `add_liquidity(asset1, asset2, amount1, amount2, ...)`. Because `reserve1` is now huge relative to `total_supply`, `mul_div(amount1, total_supply, reserve1)` rounds toward zero, and the victim receives far fewer LP tokens than the fair-value share of their deposit (or the call fails with `InsufficientLiquidityMinted`, forcing the victim to overpay proportionally to get any shares).
5. Attacker withdraws the donation-inflated reserve share back out via `remove_liquidity`, capturing value contributed by the victim.

### Citations

**File:** substrate/frame/asset-conversion/src/lib.rs (L813-814)
```rust
			let reserve1 = Self::get_balance(&pool_account, asset1.clone());
			let reserve2 = Self::get_balance(&pool_account, asset2.clone());
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L858-877)
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

			ensure!(
				lp_token_amount > T::MintMinLiquidity::get(),
				Error::<T>::InsufficientLiquidityMinted
			);
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L915-939)
```rust
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
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L1353-1368)
```rust
		pub(super) fn calc_lp_amount_for_zero_supply(
			amount1: &T::Balance,
			amount2: &T::Balance,
		) -> Result<T::Balance, Error<T>> {
			let amount1 = T::HigherPrecisionBalance::from(*amount1);
			let amount2 = T::HigherPrecisionBalance::from(*amount2);

			let result = amount1
				.checked_mul(&amount2)
				.ok_or(Error::<T>::Overflow)?
				.integer_sqrt()
				.checked_sub(&T::MintMinLiquidity::get().into())
				.ok_or(Error::<T>::InsufficientLiquidityMinted)?;

			result.try_into().map_err(|_| Error::<T>::Overflow)
		}
```
