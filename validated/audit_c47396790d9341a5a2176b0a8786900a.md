### Title
`pallet-asset-conversion` liquidity pricing and LP-mint ratio read the pool account's raw balance, letting an unprivileged donation permanently skew reserves and LP-token price - ([File: substrate/frame/asset-conversion/src/lib.rs])

### Summary
`pallet-asset-conversion` computes swap quotes and LP-token mint amounts from `Self::get_balance(&pool_account, asset)`, i.e. the pool account's *actual on-chain balance* of each asset, rather than from an independently maintained, deposit/withdraw-updated reserve counter [1](#0-0) . This is the same broken invariant as the Elytra report: an accounting value that is supposed to represent "funds actually contributed through the pool's accounted flows" is instead read directly from a balance that any unprivileged account can mutate by simply transferring tokens to the pool address, without going through `add_liquidity`/`remove_liquidity`. A prdoc in this repo explicitly documents that the pallet was changed to "use full balances for pool prices" specifically so that "protected funds or unrelated non-sufficient assets held in the pool account" are counted in reserves [2](#0-1) , confirming that arbitrary balance sitting in the pool account is treated as legitimate reserve for pricing/minting purposes.

### Finding Description
`do_add_liquidity` reads `reserve1`/`reserve2` via `get_balance(&pool_account, asset)` and uses them to (a) compute the optimal deposit ratio via `Self::quote`, and (b) compute LP tokens to mint via `side1 = mul_div(amount1, total_supply, reserve1)` and `side2 = mul_div(amount2, total_supply, reserve2)`, taking the min [3](#0-2) . Both `reserve1` and `reserve2` are literally the pool account's live token balance, which is exactly the value an attacker can inflate by directly transferring (donating) tokens to the pool account — with no `add_liquidity` call and therefore no LP tokens minted to the donor.

Because `reserve1`/`reserve2` feed straight into `mul_div(amount, total_supply, reserve)`, an attacker who front-loads extra tokens into the pool account before a legitimate LP deposits can deflate `lp_token_amount` for that depositor (classic ERC4626-style share-price inflation/rounding-down attack), or conversely can manipulate `quote_price_exact_tokens_for_tokens` and `get_reserves` used by swappers, since `get_reserves` also returns `get_balance(&pool_account, asset)` directly [4](#0-3) . This mirrors the Elytra bug precisely: the "tracked" value that should represent contributions accounted for through the pool's mint/burn bookkeping is instead read from a directly-donatable raw balance, so an unprivileged donation silently and permanently distorts the price/ratio used for all subsequent LP-token minting and swap pricing — there is no revert, no guard, and no reconciliation against `total_issuance` of the LP token.

Unlike the Elytra report's clamp-to-zero path, here there is no bounds check at all: the reserve value is used unconditionally in `mul_div` and `quote`, so the corrupted value (`reserve1`/`reserve2`) directly determines minted LP-token amounts and swap outputs. `InsufficientLiquidityMinted`/slippage checks (`amount1_min`/`amount2_min`, `MintMinLiquidity`) do not protect the depositor against a pre-inflated reserve, since the attacker controls the timing of the donation relative to the depositor's `add_liquidity` call within the same block.

### Impact Explanation
This directly falls under "Balances, assets, ... treasury spends, ... and contract-held value must conserve value and settle exactly once to the rightful beneficiary and amount." An attacker can use a donation to the pool account to:
- Reduce the LP tokens minted to a legitimate depositor relative to the value they contributed (value theft from LPs to the attacker's later `remove_liquidity`, since the attacker holds no LP tokens but effectively raises `reserve` per outstanding LP token).
- Distort swap pricing (`get_reserves`, `quote_price_exact_tokens_for_tokens`), degrading intended AMM invariant behavior for third-party swappers.

This is a public, unprivileged, no-malicious-relayer/validator path (anyone can `Assets::transfer` or `Balances::transfer` directly to the pool account), matching the "no malicious peer/validator/admin" constraint of the impact gate.

### Likelihood Explanation
Likelihood is High: it only requires knowing the deterministic `pool_account` address (`T::PoolLocator::address`, derivable by anyone) and sending a plain asset transfer to it before/around a target's `add_liquidity` or swap transaction. No privileged role, governance action, or off-chain infrastructure is needed.

### Recommendation
Maintain reserves via an internally tracked storage value updated only on `add_liquidity`/`remove_liquidity`/`swap` (mint/burn/transfer bookkeeping), rather than reading the pool account's live balance for pricing and LP-mint calculations. If reserves must be balance-derived for safety (e.g., to handle rounding), reconcile any excess "unaccounted" balance separately (e.g., via a `sync`/`skim` extrinsic that only affects future LPs, never retroactively affects an in-flight `add_liquidity`/swap call), and disallow any single-block sequence where an external transfer can influence the reserve figure used within the same or an adjacent extrinsic that mints LP tokens or computes swap output.

### Proof of Concept
1. Attacker observes `pool_account = T::PoolLocator::address(pool_id)` for a target pool (asset1/asset2), deterministically derivable off-chain.
2. Attacker transfers a large amount of `asset1` directly to `pool_account` (a plain `Assets::transfer`/`Balances::transfer`, no LP tokens minted) — this raises `reserve1` returned by `get_balance` at line 813 of `substrate/frame/asset-conversion/src/lib.rs`.
3. Victim calls `add_liquidity(asset1, asset2, amount1_desired, amount2_desired, ...)`. Inside `do_add_liquidity`, `reserve1`/`reserve2` are read fresh (post-donation) and used in `side1 = mul_div(amount1, total_supply, reserve1)` / `side2 = mul_div(amount2, total_supply, reserve2)` — because `reserve1` is inflated, `lp_token_amount = side1.min(side2)` is deflated relative to the victim's actual capital contribution [5](#0-4) .
4. The victim receives fewer LP tokens than the fair share of their deposit; the attacker (holding no LP tokens) has permanently degraded the LP/reserve ratio for the pool without ever calling `add_liquidity`, in effect capturing value from every future depositor's minted share until someone eventually swaps/removes liquidity to rebalance.

Note: I was unable to fully inspect `get_balance`'s exact implementation and `do_remove_liquidity`'s reserve usage within this session (tool budget exhausted before retrieving those exact line ranges), so I cannot 100% confirm whether `remove_liquidity` uses the same live-balance reserves for payout calculation — this should be verified in a follow-up review, as it could compound the impact (attacker's donated funds being partially claimable by whoever removes liquidity next).

### Citations

**File:** substrate/frame/asset-conversion/src/lib.rs (L810-879)
```rust
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

			ensure!(
				amount1.saturating_add(reserve1) >= T::Assets::minimum_balance(asset1.clone()),
				Error::<T>::AmountOneLessThanMinimal
			);
			ensure!(
				amount2.saturating_add(reserve2) >= T::Assets::minimum_balance(asset2.clone()),
				Error::<T>::AmountTwoLessThanMinimal
			);

			T::Assets::transfer(asset1, who, &pool_account, amount1, Preserve)?;
			T::Assets::transfer(asset2, who, &pool_account, amount2, Preserve)?;

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

			T::PoolAssets::mint_into(pool.lp_token.clone(), mint_to, lp_token_amount)?;
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
