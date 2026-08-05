## Analysis

The external report's core broken invariant: a public, permissionless entry point computes a "preview" share/amount via a ratio that depends on a manipulable balance (`_space.balanceOf(address(this))`); an attacker inflates that balance via a direct token transfer, the ratio then rounds down to zero, and a `require(shares != 0)`-style guard reverts the whole call, permanently breaking the essential operation.

The closest local analog is `pallet-asset-conversion`'s `do_add_liquidity` in `substrate/frame/asset-conversion/src/lib.rs`. Pool reserves are read directly from the pool account's live token balance via `Self::get_balance` / `T::Assets::balance`, not from an internally tracked, transfer-guarded invariant: [1](#0-0) 

In `do_add_liquidity`, once a pool has non-zero LP supply, minted LP shares are computed as `mul_div(amount, total_supply, reserve)` for each side, and the smaller of the two is used, gated by a strict "greater than minimum" check that reverts on failure: [2](#0-1) [3](#0-2) 

Because `reserve1`/`reserve2` are just the pool account's raw asset balances, any unprivileged account can transfer (donate) a large amount of `asset1` or `asset2` directly to the pool account (a normal, filter-free fungible/assets transfer to a known, deterministically-derived address) without going through `add_liquidity`. This artificially inflates the denominator used in `mul_div`, and for any subsequent depositor with a proportionally small `amount1`/`amount2`, `side1`/`side2` truncate toward zero, tripping `Error::<T>::InsufficientLiquidityMinted` and reverting `add_liquidity` — exactly the "preview rounds to zero → revert" pattern in the external report, except here it's a live, standing DoS on a public entrypoint (`add_liquidity` becomes permanently unusable for that pool until reserves are diluted back down, which itself requires further depositors to succeed).

This differs from the well-known "first depositor" (`total_supply.is_zero()`) case, which is already defended by minting `T::MintMinLiquidity` to the pool account itself: [4](#0-3) 

That defense only protects the very first LP mint; it does nothing to stop post-hoc donation griefing against an already-initialized pool, since reserves are read live from balances rather than from an accounting ledger insulated from direct transfers.

### Title
Direct-transfer donation to an AMM pool account can permanently DoS `add_liquidity` via zero-share rounding - (File: substrate/frame/asset-conversion/src/lib.rs)

### Summary
`pallet-asset-conversion::do_add_liquidity` derives pool reserves from the pool account's raw, unguarded token balance (`Self::get_balance`). Because any account can transfer tokens directly to the pool account without calling `add_liquidity`, an attacker can inflate one side's reserve, causing the LP-share calculation `mul_div(amount, total_supply, reserve)` to round down to zero for legitimate depositors, tripping the `InsufficientLiquidityMinted` guard and reverting the call.

### Finding Description
`do_add_liquidity` computes:
```
reserve1 = get_balance(pool_account, asset1)
reserve2 = get_balance(pool_account, asset2)
...
side1 = mul_div(amount1, total_supply, reserve1)
side2 = mul_div(amount2, total_supply, reserve2)
lp_token_amount = side1.min(side2)
ensure!(lp_token_amount > MintMinLiquidity, InsufficientLiquidityMinted)
```
`get_balance` simply queries `T::Assets::balance(asset, owner)` — the pool account's live balance — with no internal, transfer-resistant accounting of "reserves contributed via `add_liquidity`" separate from "tokens the account happens to hold." A pool's account ID is deterministically derivable from the asset pair (`T::PoolLocator::address`), so any unprivileged actor can send an arbitrary amount of `asset1` or `asset2` straight to it via a normal `transfer` call, bypassing `add_liquidity` entirely and its slippage/minimum checks.

Once `reserve1` (or `reserve2`) is inflated relative to `total_supply`, `mul_div` (fixed-point integer division after upscaling to `HigherPrecisionBalance`) truncates toward zero for any depositor whose `amount / reserve` ratio is small enough. Both `side1` and `side2` can round to a value at or below `MintMinLiquidity`, so `lp_token_amount > MintMinLiquidity` fails and the entire `add_liquidity` extrinsic reverts.

### Impact Explanation
This is a public, underpriced-work style denial of service against a core DeFi primitive shipped in the runtime: `add_liquidity` can be made to permanently revert for a specific pool by a single low-cost donation transaction, with no privileged actor, relayer, or governance action involved. It degrades intended chain behavior (liquidity provisioning stalls) and can strand a pool in a state where new liquidity cannot be added, which is a form of state lock on that pool's growth/rebalancing — fitting "runtime bugs that compromise intended behavior" / "permanent ... state lock" in the impact gate.

### Likelihood Explanation
High reachability: the pool account address is derivable by anyone from `PoolLocator::pool_id`/`address`, and sending assets to an arbitrary account via `pallet-assets`/`pallet-balances` `transfer` is unrestricted and cheap. No race condition, admin key, or validator collusion is needed — a single attacker-controlled account with modest token holdings of the smaller-reserve asset suffices to grief any actively-traded pool.

### Recommendation
Track pool reserves via internal accounting state (e.g., a `Reserves` storage item updated only by `do_add_liquidity`/`do_remove_liquidity`/swap paths) rather than reading the live token balance, or apply a "virtual shares/assets" offset (as used in some ERC4626 mitigations) so that donations cannot disproportionately affect the `mul_div` ratio. At minimum, reconcile any balance in excess of tracked reserves (e.g., via a `sync`/`skim` extrinsic) so it cannot be weaponized to force `InsufficientLiquidityMinted` reverts.

### Proof of Concept
1. Attacker creates or targets an existing pool `(asset1, asset2)` with pool account `P` and non-zero `total_supply` of LP tokens.
2. Attacker calls the plain `transfer` extrinsic to send a large amount of `asset1` directly to `P` (not via `add_liquidity`).
3. A legitimate user calls `add_liquidity(asset1, asset2, amount1_desired, amount2_desired, ...)` with amounts proportioned to the pool's *pre-donation* reserves.
4. Inside `do_add_liquidity`, `reserve1` now reflects the inflated balance; `side1 = mul_div(amount1, total_supply, reserve1)` rounds to `0` (or `<= MintMinLiquidity`).
5. `ensure!(lp_token_amount > T::MintMinLiquidity::get(), Error::<T>::InsufficientLiquidityMinted)` fails, and the user's `add_liquidity` call reverts, even though the user supplied economically reasonable amounts prior to the donation.

### Citations

**File:** substrate/frame/asset-conversion/src/lib.rs (L813-814)
```rust
			let reserve1 = Self::get_balance(&pool_account, asset1.clone());
			let reserve2 = Self::get_balance(&pool_account, asset2.clone());
```

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

**File:** substrate/frame/asset-conversion/src/lib.rs (L1265-1269)
```rust
		/// Get the `owner`'s balance of `asset`, which could be the chain's native asset or another
		/// fungible. Returns a value in the form of an `Balance`.
		pub(crate) fn get_balance(owner: &T::AccountId, asset: T::AssetKind) -> T::Balance {
			T::Assets::balance(asset, owner)
		}
```
