### Title
Direct token donation to an asset-conversion pool account can manipulate `reserve1`/`reserve2` used for liquidity accounting, allowing an attacker to steal value from subsequent liquidity providers - (File: `substrate/frame/asset-conversion/src/lib.rs`)

### Summary
`pallet-asset-conversion`'s `do_add_liquidity` computes the pool's reserves by directly reading the pool account's on-chain asset balances (`Self::get_balance(&pool_account, asset)`), exactly like the reported Controller.sol bug reads `balanceOf(address(this))` to determine the staked amount. Because the pool account is an ordinary account, anyone can transfer tokens to it outside of `add_liquidity`/`swap`, inflating the "reserve" the pallet uses to price new liquidity contributions without minting any corresponding LP tokens. This is the classic share/reserve donation-inflation primitive, and it reaches the pallet's core accounting invariant (LP-token share of pool value) in the same way the external report's tax bypass reached `_stake`'s accounting.

### Finding Description
In `do_add_liquidity`: [1](#0-0) 

```rust
let pool = Pools::<T>::get(&pool_id).ok_or(Error::<T>::PoolNotFound)?;
let pool_account = T::PoolLocator::address(&pool_id).map_err(|_| Error::<T>::InvalidAssetPair)?;

let reserve1 = Self::get_balance(&pool_account, asset1.clone());
let reserve2 = Self::get_balance(&pool_account, asset2.clone());
...
let amount2_optimal = Self::quote(&amount1_desired, &reserve1, &reserve2)?;
```

`reserve1`/`reserve2` are read live from `pool_account`'s balance, not from any pallet-tracked "amount contributed via add_liquidity" state. When `total_supply` (LP token issuance) is non-zero, the amount of LP tokens minted for a new depositor is: [2](#0-1) 

```rust
let side1 = Self::mul_div(&amount1, &total_supply, &reserve1)?;
let side2 = Self::mul_div(&amount2, &total_supply, &reserve2)?;
lp_token_amount = side1.min(side2);
...
ensure!(
    lp_token_amount > T::MintMinLiquidity::get(),
    Error::<T>::InsufficientLiquidityMinted
);
```

Because `reserve1`/`reserve2` are the corrupted values, an attacker can be the initial LP (minting the minimum-viable LP amount to pass `MintMinLiquidity`), then transfer ("donate") a large quantity of `asset1`/`asset2` directly to `pool_account` via `pallet_balances::transfer`/`pallet_assets::transfer` (outside the pallet's extrinsics). This inflates `reserve1`/`reserve2` without increasing `total_supply` of the LP token. Any later depositor's `mul_div(amount, total_supply, reserve)` is now computed against an artificially inflated denominator, so they receive far fewer LP tokens than their contribution is actually worth (or their tx reverts with `InsufficientLiquidityMinted` if the ratio is skewed enough, denying them entry at fair value). The attacker, holding the (comparatively) large existing LP share, can then call `remove_liquidity` and redeem a disproportionate share of the pool's now-larger balances — including the newer depositor's contributed funds and the attacker's own donation — capturing value that rightfully belongs to the second depositor.

The `MintMinLiquidity` check only guards the *first* deposit (when `total_supply.is_zero()`) from getting a negligible/dust LP-token grant; it does nothing to prevent this donation-based reserve manipulation for later `total_supply > 0` deposits. There is no invariant check (e.g., comparing pre/post `k = reserve1 * reserve2` against pallet-tracked contributions) that would detect or reject balances arriving to `pool_account` outside of `add_liquidity`/`swap`.

### Impact Explanation
This breaks the "Balances, assets... treasury spends... conserve value and settle exactly once to the rightful beneficiary and amount" invariant. An unprivileged attacker can permanently misprice and steal value from later liquidity providers of any newly created pool, without needing governance, admin, validator, or off-chain infrastructure — a pure public-entrypoint fund-theft vector against `pallet-asset-conversion`, which is deployed on Asset Hub and used by transaction-payment-in-asset flows and XCM asset swaps.

### Likelihood Explanation
Likelihood is moderate-to-high: it requires only (1) creating a pool and being its first depositor (trivial, permissionless via `create_pool`/`add_liquidity`), and (2) a plain balance transfer to the deterministic `pool_account` address (which is derivable via `T::PoolLocator::address`), both ordinary signed extrinsics available to any account. No sandwiching, front-running, or privileged access is required — this is the exact bug-class of the external report (bypassing accounting by manipulating balance before invoking the state-changing entrypoint).

### Recommendation
Do not derive `reserve1`/`reserve2` purely from `pool_account`'s live balance for LP-token minting math. Track and update pool reserves in pallet storage (e.g., alongside `Pools<T>`) on every `add_liquidity`/`remove_liquidity`/`swap`, and reconcile any excess/deficit against the tracked value rather than trusting the raw balance, or otherwise ensure the invariant `k = reserve1 * reserve2 / total_supply^2` cannot be inflated by unaccounted transfers before minting new LP shares.

### Proof of Concept
1. Attacker calls `create_pool(asset1, asset2)` and `add_liquidity` with the minimum amounts needed to pass `InsufficientLiquidityMinted` (mints `lp_token_amount` just above `MintMinLiquidity`), becoming sole LP holder.
2. Attacker sends a large direct transfer of `asset1` (and/or `asset2`) to the deterministic `pool_account` address (via `Balances::transfer`/`Assets::transfer`, not via the asset-conversion pallet) — cf. how `reserve1`/`reserve2` are read from `Self::get_balance(&pool_account, ...)`: [3](#0-2) .
3. Victim calls `add_liquidity` with a fair-market-priced `amount1_desired`/`amount2_desired`. `mul_div(&amount, &total_supply, &reserve)` at [4](#0-3)  now divides by the inflated `reserve`, producing a much smaller `lp_token_amount` than the victim's contribution warrants.
4. Attacker calls `remove_liquidity`, redeeming their LP tokens for a share of the pool computed via `Self::get_reserves` at [5](#0-4) , capturing a disproportionate amount of the pool's (now inflated) balances relative to their true LP share, at the victim's expense.

### Citations

**File:** substrate/frame/asset-conversion/src/lib.rs (L809-822)
```rust
			let pool = Pools::<T>::get(&pool_id).ok_or(Error::<T>::PoolNotFound)?;
			let pool_account =
				T::PoolLocator::address(&pool_id).map_err(|_| Error::<T>::InvalidAssetPair)?;

			let reserve1 = Self::get_balance(&pool_account, asset1.clone());
			let reserve2 = Self::get_balance(&pool_account, asset2.clone());

			let amount1: T::Balance;
			let amount2: T::Balance;
			if reserve1.is_zero() || reserve2.is_zero() {
				amount1 = amount1_desired;
				amount2 = amount2_desired;
			} else {
				let amount2_optimal = Self::quote(&amount1_desired, &reserve1, &reserve2)?;
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L868-877)
```rust
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

**File:** substrate/frame/asset-conversion/src/lib.rs (L913-920)
```rust
			let (reserve1, reserve2) = Self::get_reserves(asset1.clone(), asset2.clone())?;

			let total_supply = T::PoolAssets::total_issuance(pool.lp_token.clone());
			let withdrawal_fee_amount = T::LiquidityWithdrawalFee::get() * lp_token_burn;
			let lp_redeem_amount = lp_token_burn.saturating_sub(withdrawal_fee_amount);

			let amount1 = Self::mul_div(&lp_redeem_amount, &reserve1, &total_supply)?;
			let amount2 = Self::mul_div(&lp_redeem_amount, &reserve2, &total_supply)?;
```
