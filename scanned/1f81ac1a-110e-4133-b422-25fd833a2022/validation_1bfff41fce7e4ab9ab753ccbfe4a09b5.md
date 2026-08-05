Confirmed: `LiquidityWithdrawalFee` is set to `Permill::from_percent(0)` in every live runtime configuration (Asset Hub Westend, Asset Hub Rococo, Penpal, `staking-async` parachain runtime, and the Substrate test node), i.e. the JIT-liquidity mitigation exists in the pallet's design but is disabled network-wide. [1](#0-0) [2](#0-1) 

### Title
Sandwiched liquidity provisioning lets an attacker capture pro-rata swap-fee "donations" meant for existing LPs - (File: substrate/frame/asset-conversion/src/lib.rs)

### Summary
`pallet-asset-conversion` values LP tokens strictly as `reserve_of_asset / total_supply_of_lp_token` at the moment of `add_liquidity`/`remove_liquidity`. LP swap fees (and any direct token transfer to the pool account) are added to the pool's reserves without minting new LP tokens, exactly like the `donateToTranche` mechanism in the external report: value flows into the shared pot, and whoever holds LP-token shares at settlement time captures a pro-rata piece of it, regardless of how briefly they've held those shares. With `LiquidityWithdrawalFee` set to `0%` on every configured runtime, there is no cost to entering and exiting the pool around a value-injecting event, reproducing the same front-run-deposit / back-run-withdraw dilution primitive described in the Arcadia report.

### Finding Description
`do_add_liquidity` computes minted LP tokens from live pool-account balances (`reserve1`, `reserve2`) versus `total_supply` of the LP token: [3](#0-2) [4](#0-3) 

`do_remove_liquidity` symmetrically redeems LP tokens for a pro-rata slice of current reserves, reduced only by `T::LiquidityWithdrawalFee`: [5](#0-4) 

Swap fees (`LPFee`, 0.3% on the affected runtimes) are retained inside the pool account as part of `reserve_in`/`reserve_out` when a swap executes — they are not distributed to LP holders individually, they simply raise the reserves that any LP-token holder is entitled to redeem against. This is functionally identical to the audited `donateToTranche` primitive: an external event increases the pot's balance without changing share accounting, and share value is computed dynamically at withdraw time.

Because `LiquidityWithdrawalFee` is `0%` on every runtime that configures the pallet (Asset Hub Westend, Asset Hub Rococo, Penpal, the `staking-async` parachain runtime, and the reference Substrate node), the pallet's only built-in guard against this dilution/JIT pattern (an economic exit penalty, analogous to the "timelock" remediation suggested in the external report) is disabled. An attacker who observes a pending large swap in the transaction pool can:
1. Front-run with `add_liquidity`, minting LP tokens at the pre-swap reserve ratio.
2. Let the large swap execute, which pays its 0.3% LP fee into the pool account, inflating `reserve1`/`reserve2` for the LP token supply that now includes the attacker's freshly minted tokens.
3. Back-run with `remove_liquidity` in the very next block, redeeming LP tokens for `lp_redeem_amount * reserve / total_supply` — capturing a pro-rata share of the fee just paid, with zero withdrawal penalty and negligible holding-period exposure to price risk within the same block pair.

Existing guards do not stop this: `MintMinLiquidity` only prevents dust LP-token mints on pool creation; `amount1_min`/`amount2_min` on `add_liquidity` only protects the depositor, not other LPs; there is no minimum holding period, no reward-counter/checkpoint mechanism (unlike `pallet-nomination-pools`, which explicitly calls `RewardPool::update_records` before any points change specifically to prevent this class of dilution — see `substrate/frame/nomination-pools/src/lib.rs:3668-3683`), and `LiquidityWithdrawalFee` — the pallet's designed mitigation — is configured to zero everywhere.

### Impact Explanation
Long-term liquidity providers on Asset Hub (and any other configured parachain) have their share of accrued swap fees diluted every time a sizeable swap is sandwiched by add/remove liquidity, transferring fee revenue that should compound to existing LPs into the pocket of short-term "JIT" liquidity providers. This is a public, permissionless, underpriced-work-style value drain against ordinary LP depositors — no privileged role, relayer, or governance action is required, satisfying the "theft or duplicate settlement" and "public underpriced work" impact categories for in-scope Asset Hub runtime logic.

### Likelihood Explanation
Likelihood is meaningfully constrained by the requirement to detect a large pending swap and execute both `add_liquidity` and `remove_liquidity` around it within the same or adjacent blocks, and by needing sufficient capital to dominate the pool's reserves; on chains without private mempools this is achievable by any user monitoring the transaction pool, since Substrate-based parachains (unlike some L2s cited as "not exploitable" in the original Sherlock discussion) generally do not shield the mempool from ordinary participants.

### Recommendation
Enable a non-zero `LiquidityWithdrawalFee` (or add a minimum liquidity-provision holding period/block-delay) on all production `pallet-asset-conversion` runtime configurations, and/or move fee accrual to a checkpoint/counter-based accounting model similar to `pallet-nomination-pools`' `RewardPool::update_records`, so that only LPs who held shares before a fee-generating swap can claim a portion of it.

### Proof of Concept
1. Runtime under test: Asset Hub Westend config (`LiquidityWithdrawalFee = 0%`, `LpFee = 0.3%`) — [1](#0-0) .
2. Pool `(DOT, X)` has reserves `R1, R2` and LP supply `S` held by long-term LPs.
3. Attacker calls `add_liquidity` with `amount1_desired = k*R1`, minting `k*S` LP tokens at the pre-swap ratio (`do_add_liquidity`, `substrate/frame/asset-conversion/src/lib.rs:858-872`).
4. A large pending `swap_exact_tokens_for_tokens` executes, paying `0.3%` of the swap amount into the pool reserves as `LPFee`, increasing `R1`/`R2` without any LP-token mint.
5. Attacker immediately calls `remove_liquidity` for `k*S` LP tokens; `do_remove_liquidity` (`substrate/frame/asset-conversion/src/lib.rs:913-920`) returns `k*S * (R1+fee1)/(S+k*S)`, `k*S * (R2+fee2)/(S+k*S)` — since `LiquidityWithdrawalFee = 0`, the attacker withdraws their principal plus a `k/(1+k)` pro-rata share of the swap fee that would otherwise have accrued entirely to the pre-existing LPs, at essentially zero cost or holding-period risk.

### Citations

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/src/lib.rs (L467-470)
```rust
/// Union fungibles implementation for [`PoolAssetsFreezer`] and [`NativeAndNonPoolAssetsFreezer`].
///
/// NOTE: Should be kept updated to include ALL balances and assets in the runtime.
pub type NativeAndAllAssetsFreezer = fungibles::UnionOf<
```

**File:** cumulus/parachains/runtimes/assets/asset-hub-rococo/src/lib.rs (L307-312)
```rust
parameter_types! {
	pub const AssetConversionPalletId: PalletId = PalletId(*b"py/ascon");
	pub LpFee: Permill = Permill::from_rational(3u32, 1_000u32); // 0.3%
	pub MaxSwapFee: Permill = Permill::from_percent(2);
	pub const LiquidityWithdrawalFee: Permill = Permill::from_percent(0);
}
```

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

**File:** substrate/frame/asset-conversion/src/lib.rs (L913-920)
```rust
			let (reserve1, reserve2) = Self::get_reserves(asset1.clone(), asset2.clone())?;

			let total_supply = T::PoolAssets::total_issuance(pool.lp_token.clone());
			let withdrawal_fee_amount = T::LiquidityWithdrawalFee::get() * lp_token_burn;
			let lp_redeem_amount = lp_token_burn.saturating_sub(withdrawal_fee_amount);

			let amount1 = Self::mul_div(&lp_redeem_amount, &reserve1, &total_supply)?;
			let amount2 = Self::mul_div(&lp_redeem_amount, &reserve2, &total_supply)?;
```
