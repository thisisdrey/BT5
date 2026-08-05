Confirmed analog: `do_add_liquidity` in `pallet-asset-conversion` reads pool reserves via a plain `Assets::balance` query on a normal, unrestricted account, exactly like the report's core flaw ("attacker can inflate a pool address's balance out-of-band, before a state-dependent invariant is computed"). The pool account is a deterministic, publicly-known `AccountId` (`PoolLocator::address`), and nothing prevents any account from transferring assets directly to it outside of `add_liquidity`/`swap`. The existing tests (`cannot_block_pool_creation`, `add_tiny_liquidity_directly_to_pool_address`) only prove that *pool creation* and *first LP mint on an unbalanced/one-sided donation* survive dust-DoS; they do not test the classic **two-sided pre-donation price-manipulation** scenario for the very first liquidity provision.

### Title
First-liquidity-provision price manipulation via unrestricted direct donations to the pool account - (File: `substrate/frame/asset-conversion/src/lib.rs`)

### Summary
`Pallet::do_add_liquidity` computes the LP-minting ratio from `Self::get_balance(&pool_account, asset)`, i.e. the *raw* asset balance of the deterministic pool account [1](#0-0) . Because the pool account is just a normal `AccountId` derived from `PoolLocator::address`, any unprivileged account can transfer assets into it directly via `pallet_assets`/`pallet_balances` transfer extrinsics, bypassing `add_liquidity` entirely — mirroring the report's core primitive: inflating a not-yet-initialized pool's on-chain balance out-of-band before the protected invariant (in Uniswap, `uniswapV3MintCallback`'s balance delta check; here, the reserve ratio used for optimal-amount quoting) is evaluated.

### Finding Description
In `do_add_liquidity`, once `reserve1`/`reserve2` are both non-zero, the function no longer treats the deposit as the "first" provision and instead computes an "optimal" second-asset amount from the current ratio via `Self::quote` [2](#0-1) . An attacker can front-run the pool creator's real first deposit by:
1. Waiting for `create_pool` (permissionless, cheap) to register `pool_account`.
2. Directly transferring a tiny, attacker-chosen ratio of `asset1`/`asset2` straight into `pool_account` using ordinary `Assets::transfer`/`Balances::transfer` — no `add_liquidity` call needed, so `total_supply` of the LP token stays `0`.
3. When the legitimate LP provider calls `add_liquidity` with their real desired amounts, `reserve1`/`reserve2` are non-zero (set by the attacker's donation), so the `else` branch executes and `quote()` recomputes `amount2`/`amount1` against the attacker-controlled ratio instead of the victim's intended price, silently truncating the victim's deposit to the attacker's ratio (`AssetTwoDepositDidNotMeetMinimum`/`OptimalAmountLessThanDesired` guard against total failure but not against an unfavorable, attacker-dictated exchange rate).

The `MintMinLiquidity` mechanism [3](#0-2)  only guards against the classic "share inflation" attack on LP-token minting math when `total_supply` transitions from zero — it does nothing to stop the reserve values themselves from being pre-seeded by an outsider before that transition happens, since `calc_lp_amount_for_zero_supply` uses `amount1`/`amount2` (the just-computed, already-corrupted deposit amounts), not a value independent of the donation.

This is the direct structural analog of the external report: a state value (`reserve1`/`reserve2`, analogous to Uniswap's `balance0()`/`balance1()`) that is trusted to reflect only protocol-mediated deposits is actually a raw balance query on an address anyone can fund unconditionally, allowing an attacker to corrupt the invariant computation of a subsequent privileged operation (`add_liquidity`).

### Impact Explanation
The victim's liquidity deposit is silently priced at a ratio the attacker chose (via a near-zero-cost donation), rather than the pool's true first-market price. This lets an attacker force LP providers to deposit at a manipulated ratio, effectively stealing value from the victim's deposit or later extracting it via a swap against the artificially seeded ratio. This falls under "public underpriced work / manipulation of pool state that degrades intended AMM invariants" and "conserve value and settle exactly once to the rightful beneficiary and amount" from the impact gate, since the LP token amount and asset split credited to the victim no longer reflect the price they intended.

### Likelihood Explanation
Likelihood is Medium: it requires no privileged role, only (a) knowledge that a pool was just created (public event `PoolCreated`) and (b) a plain asset transfer to the deterministic pool account before the victim's `add_liquidity` transaction lands — a standard front-running/same-block ordering condition available to any unprivileged actor, not a validator/collator/relayer.

### Recommendation
Do not derive the "is this the pool's first liquidity provision" branch, nor the optimal-amount quoting ratio, from the raw `Assets::balance` of `pool_account`. Track reserves (or at least "has liquidity ever been added") in pallet storage (e.g., alongside `PoolInfo`/`Pools`) so unsolicited direct transfers to the pool account cannot influence the quoting logic, and reject/ignore balance that was not deposited through `do_add_liquidity`/`do_swap_*`.

### Proof of Concept
1. `create_pool(asset1, asset2)` — pool account `P` exists, `total_supply(lp_token) == 0`.
2. Attacker (any account) calls `Assets::transfer(asset1, attacker, P, 1)` and `Assets::transfer(asset2, attacker, P, 1000)` directly (or the native-currency equivalent), setting `reserve1 = 1`, `reserve2 = 1000`.
3. Victim submits `add_liquidity(asset1, asset2, amount1_desired = 1000, amount2_desired = 1000, amount1_min = 0, amount2_min = 0, victim)` expecting a roughly 1:1 deposit.
4. Because `reserve1`/`reserve2` are non-zero, `do_add_liquidity` takes the `else` branch: `quote(1000, 1, 1000) = 1_000_000`, exceeding `amount2_desired`; it falls to the `amount1_optimal = quote(1000, 1000, 1) = 1` branch, so the victim's actual deposit is silently reduced to `amount1 = 1, amount2 = 1000` — i.e., the attacker dictated the victim's effective contribution ratio using a 1001-unit donation, confirmed by [4](#0-3) .

### Citations

**File:** substrate/frame/asset-conversion/src/lib.rs (L813-814)
```rust
			let reserve1 = Self::get_balance(&pool_account, asset1.clone());
			let reserve2 = Self::get_balance(&pool_account, asset2.clone());
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L816-844)
```rust
			let amount1: T::Balance;
			let amount2: T::Balance;
			if reserve1.is_zero() || reserve2.is_zero() {
				amount1 = amount1_desired;
				amount2 = amount2_desired;
			} else {
				let amount2_optimal = Self::quote(&amount1_desired, &reserve1, &reserve2)?;

				if amount2_optimal <= amount2_desired {
					ensure!(
						amount2_optimal >= amount2_min,
						Error::<T>::AssetTwoDepositDidNotMeetMinimum
					);
					amount1 = amount1_desired;
					amount2 = amount2_optimal;
				} else {
					let amount1_optimal = Self::quote(&amount2_desired, &reserve2, &reserve1)?;
					ensure!(
						amount1_optimal <= amount1_desired,
						Error::<T>::OptimalAmountLessThanDesired
					);
					ensure!(
						amount1_optimal >= amount1_min,
						Error::<T>::AssetOneDepositDidNotMeetMinimum
					);
					amount1 = amount1_optimal;
					amount2 = amount2_desired;
				}
			}
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
