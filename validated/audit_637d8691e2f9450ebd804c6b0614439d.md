### Title
LP share price manipulation via direct asset donation to `AssetConversion` pool account causes depositor fund loss through rounding - (File: `substrate/frame/asset-conversion/src/lib.rs`)

### Summary
`pallet-asset-conversion` computes pool reserves by reading the *live* asset balance of the pool account (`Self::get_balance`) rather than from an internally tracked reserve value that is only mutated by `add_liquidity`/`remove_liquidity`/`swap`. Because LP token issuance (`total_supply`) and pool value (`reserve1`/`reserve2`) can therefore be independently manipulated — anyone can transfer (donate) assets directly to the deterministic pool account without minting LP tokens — an attacker can inflate the reserve-per-share ratio and cause later liquidity providers to receive disproportionately few LP tokens due to floor-division rounding, exactly analogous to the ERC4626 "donation" first-depositor attack described in the external report.

### Finding Description
In `do_add_liquidity`, reserves are fetched as raw balances of the pool account rather than pallet-tracked state: [1](#0-0) 

LP tokens minted for non-initial deposits are computed proportionally to `total_supply` versus these live reserves, using floor (integer) division, and only gated by a fixed absolute minimum (`MintMinLiquidity`), not a proportional guard: [2](#0-1) 

`do_remove_liquidity` uses the same live-balance reserves to compute withdrawal amounts: [3](#0-2) 

Because `reserve1`/`reserve2` are just `T::Assets::balance(asset, pool_account)`, any account — not only LPs — can transfer assets directly into the deterministic pool account (`T::PoolLocator::address`) at any time during the pool's life, inflating the reserves without touching `total_issuance` of the LP token. This is a recognized possible action against this pallet's pool accounts, demonstrated by the existing `cannot_block_pool_creation` test, which shows an attacker directly transferring tokens/ED into the not-yet-created pool account to try to manipulate/block pool setup — confirming the account is a public, unprotected destination for arbitrary donations even outside of `add_liquidity`.

Once `reserve/total_supply` has been inflated by such a donation (LP `total_supply` stays low while reserves are pumped), a legitimate depositor calling `add_liquidity` gets `lp_token_amount = floor(amount * total_supply / reserve)`. The floor-rounding loses up to (but not including) one whole LP token's worth of value — but because reserve-per-share is inflated, one LP token can represent an arbitrarily large amount of the underlying asset. The rounded-down remainder of the depositor's contributed assets is absorbed into the pool's reserves and is redistributed pro-rata to *existing* LP-token holders (i.e., the attacker, who holds the bulk of the pre-donation supply, since `T::MintMinLiquidity` permanently locks only a small fixed constant to the pool account, not a share proportional to the manipulation). The attacker can then call `remove_liquidity` to redeem their small LP-token balance against the now-larger reserve, extracting value contributed by the later depositor.

This mirrors the core broken invariant from the external report: total-supply of shares/LP-tokens can be kept artificially low while the "assets"/reserves backing them are inflated out-of-band (via direct token transfer rather than the minting entrypoint), corrupting the `reserve / total_supply` exchange rate used for both minting and redemption, and causing rounding-driven value transfer from later depositors to the party who controls the low-supply/high-reserve state.

### Impact Explanation
This breaks the "Balances, assets... conserve value and settle exactly once to the rightful beneficiary and amount" pivot: an unprivileged attacker can, without needing a malicious peer/validator/relayer, cause a legitimate liquidity provider's contributed assets to be partially misappropriated through integer-rounding on an attacker-inflated reserve ratio. Because `pallet-asset-conversion` is deployed on Asset Hub runtimes (confirmed via `AssetConversionPalletId`/`MintMinLiquidity` config wiring in `substrate/bin/node/runtime/src/lib.rs` and the Asset-Hub emulated tests), this is a live-scope, non-privileged, fund-loss issue in a public, permissionless AMM pallet.

### Likelihood Explanation
The attack requires only: (1) becoming an early/first LP with a legitimate, small `add_liquidity` call that clears the fixed `MintMinLiquidity` (100 units) threshold, and (2) an ordinary asset/balance `transfer` extrinsic sending funds directly to the deterministic, publicly-known pool account. Both steps use standard, unprivileged extrinsics available to any signed account; no governance, admin, validator, or off-chain infrastructure is needed. The pool account address is deterministically derivable by anyone via `PoolLocator`, as already demonstrated by the existing `cannot_block_pool_creation` test.

### Recommendation
Track pool reserves as pallet-internal storage state that is mutated only by `add_liquidity`, `remove_liquidity`, and `swap` (à la Uniswap V2's `reserve0`/`reserve1` plus an explicit, rate-limited `sync`/`skim`), instead of trusting the live balance of the pool account. Additionally, consider requiring a minimum proportional LP mint (not just a fixed absolute `MintMinLiquidity`) relative to the ratio change since the last recorded reserve, and/or bounding how far live balance can diverge from tracked reserve before requiring an explicit, guarded reconciliation step, mirroring the "guarded launch" mitigation recommended in the original ERC4626 report.

### Proof of Concept
1. Attacker (Alice) creates a pool for `(Native, AssetX)` and calls `add_liquidity` with amounts just above the mint threshold (e.g., providing `201`/`201`), receiving `101` LP tokens (`sqrt(201*201) - MintMinLiquidity(100) = 101`), while `100` LP tokens are permanently locked to the pool account per `do_add_liquidity` (`substrate/frame/asset-conversion/src/lib.rs:858-877`). `total_supply = 201`.
2. Alice computes the deterministic pool account address via `T::PoolLocator::address` (same mechanism exercised in the `cannot_block_pool_creation` test) and sends a large, ordinary `transfer` of `Native` and `AssetX` directly to that account (e.g., `1_000_000_000` of each), bypassing `add_liquidity` entirely. `total_supply` is unaffected; `reserve1`/`reserve2` jump to `~1_000_000_201` each because `get_balance` reads live balance (`substrate/frame/asset-conversion/src/lib.rs:813-814`).
3. Victim (Bob) calls `add_liquidity` with a modest, real contribution matching the now-inflated ratio. `lp_token_amount = floor(amount * 201 / reserve)` truncates a meaningful fraction of Bob's contribution's proportional share due to the inflated reserve, minting Bob fewer LP tokens than his contribution's true share of the resulting pool (`substrate/frame/asset-conversion/src/lib.rs:868-877`).
4. Alice calls `remove_liquidity` for her `101` LP tokens; her redemption amount is computed as `floor(lp_redeem_amount * reserve / total_supply)` (`substrate/frame/asset-conversion/src/lib.rs:915-921`) against the reserve inflated partly by her own donation and partly by Bob's under-compensated deposit, letting her recapture value disproportionate to her original `201`-unit contribution.

Note: I was not able to fully execute this scenario in a live test harness (no code execution available in this environment); the analysis is derived from static reading of `do_add_liquidity`/`do_remove_liquidity`/`get_balance` and the existing `cannot_block_pool_creation` test that already demonstrates direct-donation reachability to the pool account. A background Devin session with test-execution access would be needed to empirically confirm the exact numeric loss bounds.

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

**File:** substrate/frame/asset-conversion/src/lib.rs (L915-921)
```rust
			let total_supply = T::PoolAssets::total_issuance(pool.lp_token.clone());
			let withdrawal_fee_amount = T::LiquidityWithdrawalFee::get() * lp_token_burn;
			let lp_redeem_amount = lp_token_burn.saturating_sub(withdrawal_fee_amount);

			let amount1 = Self::mul_div(&lp_redeem_amount, &reserve1, &total_supply)?;
			let amount2 = Self::mul_div(&lp_redeem_amount, &reserve2, &total_supply)?;

```
