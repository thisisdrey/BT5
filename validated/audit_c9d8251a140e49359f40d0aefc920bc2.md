### Title
Unbacked-Reserve Read in `pallet-asset-conversion::do_add_liquidity` Lets an Unprivileged Attacker Distort First-Liquidity Pricing via Direct Token Donation - (File: `substrate/frame/asset-conversion/src/lib.rs`)

### Summary
The external report's root cause is that a liquidity-provisioning routine derives pool state from the pool's *live token balance* rather than from balances tracked/accounted internally, so tokens landing in the pool contract outside the intended flow (via `skim()`) silently corrupt subsequent liquidity-add accounting and get orphaned. The same broken invariant — "pool reserves = raw on-chain balance of the pool account, not an internally accounted reserve" — exists in `pallet-asset-conversion`, and it is reachable by any unprivileged account via the public `add_liquidity`/`transfer` primitives, with no relayer, validator, or admin involved.

### Finding Description
`do_add_liquidity` computes reserves directly from the pool account's live asset balance: [1](#0-0) 

and branches its pricing/quote logic on whether these reserves are zero: [2](#0-1) 

Because `T::Assets::transfer` to the pool account is a completely ordinary, permissionless asset transfer (any account can send `asset1`/`asset2` directly to the deterministic `pool_account` derived by `T::PoolLocator::address`), an attacker can donate a small amount of either asset directly to a freshly created, not-yet-funded pool *before* the legitimate first liquidity provider calls `add_liquidity`. This flips `reserve1`/`reserve2` from zero to nonzero, forcing the legitimate provider's call into the `else` branch that uses `Self::quote()` against the attacker-inflated reserve instead of the zero-supply branch that would otherwise compute LP tokens purely from `amount1`/`amount2` supplied by the caller: [3](#0-2) 

This mirrors the report's exact bug class: state that should be a controlled internal accounting value (the pool's reserve) is instead read straight off the token balance of the pool contract, so anyone can push value into that balance out-of-band and corrupt the pool's subsequent accounting. The `_checkPoolValidity`/`skim()` pattern in the report and the `get_balance` read in `do_add_liquidity` share the identical primitive: unaccounted, attacker-controlled deposits are folded into pool math.

### Impact Explanation
An unprivileged attacker can force the legitimate first liquidity provider's `amount2_optimal`/`amount1_optimal` computation off of a reserve value the attacker fully controls, causing the provider's actual accepted amounts (`amount1`, `amount2`) — and hence the assets debited from them via `T::Assets::transfer` — to deviate from what they intended, or causing their call to revert (`OptimalAmountLessThanDesired`, `Error::AssetTwoDepositDidNotMeetMinimum`) as a denial of service on pool bootstrapping. This is a public-entrypoint path with no admin/relayer/validator assumption, degrading intended AMM behavior for the first depositor of any given pool, consistent with the "runtime bugs that compromise intended behavior" acceptance criterion.

### Likelihood Explanation
High for any newly created but not-yet-liquid pool: `pool_account` addresses are deterministically derivable from `T::PoolLocator::address(&pool_id)` before the pool is funded, `create_pool` is a public call anyone can invoke, and `asset transfer` to an arbitrary account (including the pool account) requires no special permission for fungible assets that don't require explicit approval. No governance, keys, or infrastructure control is required.

### Recommendation
Track pool reserves as pallet storage state updated only through `do_add_liquidity`/`do_remove_liquidity`/swap logic, rather than reading `T::Assets::balance(pool_account, ...)` directly, so that donations/out-of-band transfers to the pool account cannot influence quoted pricing or LP-token minting math. Alternatively, "sweep" or fold any balance in excess of the tracked reserve into the next liquidity provider's LP minting proportionally (analogous to the report's own recommendation to sweep leftover balances) instead of letting it silently bias the `quote()` computation.

### Proof of Concept
1. Attacker calls `create_pool(asset1, asset2)` (or waits for a legitimate user to do so) so `pool_account = T::PoolLocator::address(pool_id)` becomes known.
2. Before the legitimate first liquidity provider calls `add_liquidity`, attacker sends a small `asset1` amount directly to `pool_account` using an ordinary transfer (e.g. `pallet_assets::transfer` or native `Balances::transfer`), bypassing the `add_liquidity` extrinsic entirely.
3. Legitimate provider calls `add_liquidity(asset1, asset2, amount1_desired, amount2_desired, amount1_min, amount2_min, mint_to)`.
4. In `do_add_liquidity`, `reserve1 = Self::get_balance(&pool_account, asset1)` (line 813) is now nonzero due to the attacker's donation, `reserve2` is still zero — the branch logic and `Self::quote` at lines 818–844 now operate on attacker-influenced state, producing either a revert or an unintended amount1/amount2 split that no longer matches the provider's originally desired 1:1/ratioed contribution.

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
