### Title
Direct dust transfers to a pool account can permanently DoS `add_liquidity` via the `MintMinLiquidity` check - (File: `substrate/frame/asset-conversion/src/lib.rs`)

### Summary
`pallet-asset-conversion`'s `do_add_liquidity` computes pool reserves by reading the *live token balance* of the pool's deterministic account rather than an internally accounted reserve variable, then requires the freshly minted LP amount to exceed `T::MintMinLiquidity`. Because the pool account address is derivable and any account can transfer tokens to it directly (bypassing `add_liquidity`), an attacker can inflate the reserve balances relative to `total_supply` of LP tokens. This mirrors the LoopFi `StakingLPEth` bug exactly: dust donated directly to the vault/pool inflates the assets-per-share ratio so that a legitimate depositor's computed share/LP amount rounds down below the minimum-shares floor, and the transaction reverts with `InsufficientLiquidityMinted`, denying deposits for anyone whose contribution is not large enough to overcome the attacker-inflated ratio.

### Finding Description
Reserves are fetched via `Self::get_balance(&pool_account, asset)` / `Self::get_reserves`, i.e., the *actual on-chain balance* of the pool account: [1](#0-0) 

For a pool that already has liquidity (`total_supply` non-zero), the LP tokens to mint are computed proportionally to the current reserves: [2](#0-1) 

and the call reverts unless the minted amount strictly exceeds the protocol-wide minimum liquidity floor: [3](#0-2) 

Because `reserve1`/`reserve2` are derived from the pool account's raw asset balance rather than from an internally tracked, mint/burn-gated accumulator, any account can inflate them by directly transferring `asset1`/`asset2` to the pool account (an address deterministically derivable via `T::PoolLocator::address`) without calling any pallet extrinsic and without minting any LP tokens. This raises `reserve1`/`reserve2` while `total_supply` stays unchanged, so for any subsequent depositor:

`lp_token_amount = min(amount1 * total_supply / reserve1, amount2 * total_supply / reserve2)`

shrinks. If the attacker donates enough dust relative to the existing reserves, a legitimate depositor providing an otherwise reasonable amount produces `lp_token_amount <= T::MintMinLiquidity::get()`, and `do_add_liquidity` unconditionally reverts with `InsufficientLiquidityMinted`. This is the exact "DoS of deposit/mint functionality" scenario described in the LoopFi report, where dust donated to the vault/pool inflates the assets-per-share ratio and blocks minting below the minimum-shares floor.

Note that the *first* depositor (when `total_supply.is_zero()`) is not vulnerable to this specific path, since `calc_lp_amount_for_zero_supply` depends only on the amounts the first depositor provides, not on existing reserves. The vulnerability applies to reserve-proportional minting for any pool that already has liquidity — an attacker can donate dust at any point after the first liquidity add to degrade or block subsequent deposits.

### Impact Explanation
This is public underpriced work with chain/user impact: an unprivileged attacker, with no elevated role, admin rights, or off-chain assumptions, can permanently deny normal users the ability to add liquidity to an existing pool by sending a small, cheap donation of the pool's assets directly to its account. This blocks legitimate `add_liquidity` calls (reverting with `InsufficientLiquidityMinted`), degrading a core AssetHub DeFi primitive and potentially locking users out of providing liquidity unless they deposit disproportionately large amounts to overcome the skewed ratio — directly analogous to the Medium-severity LoopFi finding.

### Likelihood Explanation
Likelihood is high for any pool where reserves are small relative to the token's decimals (e.g., freshly created pools, or pools that have been drained close to `MintMinLiquidity`), since the attacker only needs a small, self-funded transfer to any known/derivable pool account — no governance, validator, relayer, or privileged actor is required. The attack is repeatable and cheap (bounded by transaction fees), matching the "public underpriced work" criterion.

### Recommendation
Do not derive reserves from the live raw balance of the pool account for LP-minting math. Track reserves via an internal storage value updated only by successful `add_liquidity`/`remove_liquidity`/`swap` calls, so that unsolicited direct transfers to the pool account cannot influence the LP-token minting ratio. Alternatively (as LoopFi's own mitigation suggests), pre-fund/seed each new pool with the `MintMinLiquidity` amount at creation time so that reserve-to-supply ratios cannot be pushed below the minting floor by a dust donation, and consider making the `InsufficientLiquidityMinted` check tolerant of minor reserve/supply skew rather than an outright revert.

### Proof of Concept
1. Attacker or a user creates a pool for `asset1`/`asset2` and calls `add_liquidity` once with a small amount, establishing `total_supply > 0` and small reserves (e.g., `reserve1 = 100`, `reserve2 = 100`, `total_supply = 100`).
2. Attacker computes the pool account address via `T::PoolLocator::address(&pool_id)` (deterministic, public) and directly transfers, e.g., `10_000` units of `asset1` to that account using a normal `Assets::transfer` — this does not go through `do_add_liquidity`, so `total_supply` is untouched, but `reserve1` jumps to `10_100`.
3. A legitimate user now calls `add_liquidity` with a normal amount, e.g., `amount1_desired = 1000`. The computed `side1 = amount1 * total_supply / reserve1 = 1000 * 100 / 10_100 ≈ 9`, which is likely `<= T::MintMinLiquidity::get()`.
4. `do_add_liquidity` reverts with `Error::<T>::InsufficientLiquidityMinted` (`substrate/frame/asset-conversion/src/lib.rs:874-877`), blocking the legitimate deposit until the user provides a disproportionately large amount to overcome the attacker-inflated reserve.

### Citations

**File:** substrate/frame/asset-conversion/src/lib.rs (L810-815)
```rust
			let pool_account =
				T::PoolLocator::address(&pool_id).map_err(|_| Error::<T>::InvalidAssetPair)?;

			let reserve1 = Self::get_balance(&pool_account, asset1.clone());
			let reserve2 = Self::get_balance(&pool_account, asset2.clone());

```

**File:** substrate/frame/asset-conversion/src/lib.rs (L868-872)
```rust
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
