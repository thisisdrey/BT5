## Analysis

The Stargate bug's core broken invariant is: **pool balance is read via a live external `balanceOf()` call instead of an internally tracked ledger, so tokens sent directly to the pool address (bypassing the deposit entrypoint) inflate the balance used for exchange-rate/fee math**.

The direct structural analog in this repo is `pallet-asset-conversion`. Its `get_balance`/`get_reserves` helpers read the pool's *live* fungible balance instead of an internally tracked reserve, and the pool account address is a deterministic, publicly-known derivation (`PoolLocator::pool_address`) that anyone can transfer tokens to directly, outside of `add_liquidity`. [1](#0-0) [2](#0-1) 

These raw reserves feed directly into LP-share minting math in `add_liquidity`: [3](#0-2) [4](#0-3) 

and into `remove_liquidity`'s payout math and swap pricing (`get_amount_in`/`get_amount_out`), all of which trust `reserve1`/`reserve2` as ground truth: [5](#0-4) [6](#0-5) 

Note also `prdoc/pr_12408.prdoc` confirms this reserve-balance approach was already patched once (switching from reducible to full balance) but the fundamental design — trusting a live queryable balance rather than an internal deposit ledger — remains unchanged, which is the exact bug class flagged in the external report.

### Title
Pool reserves in `pallet-asset-conversion` are derived from live account balance rather than an internal ledger, allowing donation-based reserve inflation to corrupt LP-share and swap accounting - (File: `substrate/frame/asset-conversion/src/lib.rs`)

### Summary
`pallet_asset_conversion::Pallet::get_balance` calls `T::Assets::balance(asset, owner)` directly on the pool account, and `get_reserves` uses this as the sole source of truth for reserve1/reserve2. Because the pool account address is deterministically derived and public (`PoolLocator::pool_address`), any account can transfer tokens directly to it, outside of `add_liquidity`/`swap`, inflating the on-chain balance the pallet interprets as "reserve". This is the same broken invariant as the reported Stargate `_getPoolBalance` bug: trusting a raw balance query instead of an internally tracked accounting value.

### Finding Description
`get_balance`/`get_reserves` read the pool account's live fungible balance with no cross-check against an internally maintained reserve/ledger value: [1](#0-0) [2](#0-1) 

`add_liquidity` uses these reserves to compute the LP-token mint amount for non-first depositors as `min(amount1*total_supply/reserve1, amount2*total_supply/reserve2)`: [4](#0-3) 

An attacker who transfers extra tokens of `asset1`/`asset2` directly into the pool account (a normal `transfer`, not `add_liquidity`) inflates `reserve1`/`reserve2` without minting any corresponding LP tokens. Any subsequent legitimate depositor's `lp_token_amount` is then computed against the inflated reserve, so they receive fewer LP tokens than their deposit is actually worth — the excess value is absorbed into the pool and effectively redistributed to whoever holds existing LP tokens (or permanently stranded, since it was never credited as reserve1/reserve2 "added" by any liquidity provider action). The same corrupted `reserve1`/`reserve2` values are reused unconditionally in `remove_liquidity`'s payout calculation and in swap pricing (`get_amount_in`/`get_amount_out` via `balance_path_from_amount_in`), so every subsequent LP mint, LP redemption, and swap quote in that pool is derived from a value an unprivileged actor can freely manipulate with a plain token transfer.

Unlike the Stargate patch note in `prdoc/pr_12408.prdoc` (which only changed *which* live balance is read — full vs reducible — not whether a live balance is trusted at all), there is no internal, donation-resistant ledger maintained per pool; `Pools<T>` storage tracks only the LP asset id, not deposited reserve amounts.

### Impact Explanation
This breaks the "conserve value and settle exactly once to the rightful beneficiary and amount" invariant for pool accounting. A depositor can be made to receive systematically fewer LP shares than the value they deposited, and swappers receive systematically worse prices than the pool's genuine liquidity would offer — both are unpriced-value-transfer bugs stemming from a manipulable, unauthenticated "reserve" input, not from any privileged actor. Because `pallet-asset-conversion` mints/burns real asset balances (native currency and `pallet-assets` tokens) on Asset Hub, this is a direct fund-accounting corruption, not merely a display issue.

### Likelihood Explanation
The attack requires only a permissionless `transfer` (or equivalent deposit) to a deterministically-derivable pool account — no governance, no validator/collator collusion, no leaked keys, and no reliance on front-running a specific transaction (the donation can be made at any time and persists for all future interactions with the pool). This matches the required "public underpriced work" / "wrong beneficiary or amount" impact class with an unprivileged attacker primitive.

### Recommendation
Maintain an internal, pallet-storage-tracked reserve (or "local credit") per pool, updated only by `add_liquidity`, `remove_liquidity`, and swap execution, rather than deriving reserves from a live `T::Assets::balance` query. Any discrepancy between the tracked reserve and the actual account balance (from donations) should be handled explicitly — either ignored for pricing/minting purposes or swept separately — so LP-share minting, LP-share redemption, and swap pricing are computed against manipulation-resistant state, mirroring the fix pattern described in the external report (internal balance record decoupled from `balanceOf`).

### Proof of Concept
1. Pool P exists for `(asset1, asset2)` with genuine reserves `R1, R2` and LP `total_supply = S`.
2. Attacker calls a plain `Assets::transfer`/`Balances::transfer` sending `D1` of `asset1` directly to `PoolLocator::pool_address(asset1, asset2)` (a publicly computable address) — no interaction with `pallet-asset-conversion` at all.
3. `get_reserves` now returns `reserve1 = R1 + D1` (per `substrate/frame/asset-conversion/src/lib.rs:1499-1514`).
4. A legitimate LP calls `add_liquidity(asset1, asset2, amount1_desired, amount2_desired, ...)`. Because `total_supply` is non-zero, `lp_token_amount = min(amount1*S/(R1+D1), amount2*S/R2)` (per lines 858-872) — strictly smaller than it would be without the donation, i.e., the depositor is under-minted relative to the value they contributed.
5. Repeating step 2 before any deposit lets the attacker degrade minting for every subsequent depositor into that pool, with the value effectively captured by existing LP holders/pool imbalance rather than the depositor who paid for it.

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

**File:** substrate/frame/asset-conversion/src/lib.rs (L913-920)
```rust
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

**File:** substrate/frame/asset-conversion/src/lib.rs (L1309-1311)
```rust
				let (reserve_in, reserve_out) = Self::get_reserves(asset1.clone(), asset2.clone())?;
				balance_path.push((asset2, amount_in));
				amount_in = Self::get_amount_in(fee, &amount_in, &reserve_in, &reserve_out)?;
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
