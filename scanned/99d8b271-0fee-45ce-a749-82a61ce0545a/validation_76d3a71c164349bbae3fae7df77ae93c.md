### Title
LP share dilution via direct token donation to the pool account bypassing `add_liquidity` accounting - (File: `substrate/frame/asset-conversion/src/lib.rs`)

### Summary
`pallet-asset-conversion`'s `do_add_liquidity` prices new liquidity using the **live, on-chain balance** of the pool's sovereign account (`reserve1`/`reserve2`) rather than an internally tracked reserve counter that is only updated on `mint`/`burn`/`swap`. Because the pool account is a normal, keyless `AccountId` (derived via `AccountIdConverter`) that anyone can transfer tokens to directly (via `pallet-balances::transfer` or `pallet-assets::transfer`), an attacker can inflate `reserve1`/`reserve2` without minting any corresponding LP tokens. This is the on-chain analog of the StakedCitadel/TOB-YEARN-003 "donation to inflate price-per-share" primitive: whoever deposits after the donation receives a disproportionately small LP-token allocation for the value they contribute, transferring value to the earlier LP holder(s).

### Finding Description
In `do_add_liquidity`: [1](#0-0) 
`reserve1`/`reserve2` are fetched via `Self::get_balance(&pool_account, ...)`, i.e. the pool account's actual token balance — not a pallet-tracked reserve that only changes through `do_add_liquidity`/`do_remove_liquidity`/`swap`.

When liquidity already exists, the amount of LP tokens minted for a new depositor is: [2](#0-1) 
`lp_token_amount = min(amount1 * total_supply / reserve1, amount2 * total_supply / reserve2)`.

Because `reserve1`/`reserve2` are read directly from the pool account's live balance, any account can call `Balances::transfer_keep_alive` (for the native asset) or `Assets::transfer` (for the second asset) straight to the pool's sovereign account — computed purely from `PalletId` + `PoolId` via `AccountIdConverter`, requiring no permission — and inflate `reserve1`/`reserve2` without touching `total_supply` (the LP token issuance, which is only affected by `mint_into`/`burn_from` inside this pallet). This is functionally identical to the StakedCitadel bug: the attacker becomes (or already is) a large LP holder with a small `total_supply`, then "directly transfers" tokens to the vault/pool account to blow up the implied value of a unit of pool-share, so any subsequent depositor's `amount * total_supply / reserve` rounds down to a much smaller LP allocation than their fair share, while the attacker's existing LP tokens absorb the donated value.

The pallet's only defenses — `MintMinLiquidity` (guards the very first mint from being too small) and `ensure!(lp_token_amount > T::MintMinLiquidity::get(), Error::InsufficientLiquidityMinted)` — do not prevent this: `MintMinLiquidity` only bounds the initial supply floor and the minimum LP amount minted per call, it does not bind `reserve1`/`reserve2` to anything except the account's live balance, so a large-enough donation still lets a legitimate subsequent depositor clear the `MintMinLiquidity` bar while still receiving far fewer LP tokens than their deposit is worth relative to the pool's real value.

### Impact Explanation
A depositor who calls `add_liquidity` after an attacker has donated tokens straight to the pool account will have real assets debited from their account (`T::Assets::transfer(... who, &pool_account, amount1/2 ...)`) but receive an LP-token mint that under-represents their contribution, permanently losing value to whichever account already holds LP tokens (the attacker). Because this exploits public, unprivileged transfer calls plus the public `add_liquidity` extrinsic, it satisfies "theft or unbacked mint/unlock" and "public underpriced work" style impacts on real user funds without needing any privileged, admin, or malicious-infrastructure actor.

### Likelihood Explanation
The precondition — an attacker holding LP tokens in a pool with low `total_supply` (e.g. a freshly created pool, or one where most LPs have withdrawn) and being able to transfer tokens directly to a fully public, permissionless, derivable sovereign account — is trivially satisfiable by any unprivileged user at any time; no governance, validator, or relayer participation is required. The only friction is the cost of the donation itself, which is a normal economic trade-off in this class of bug (same as the original H-03 report), not a security control.

### Recommendation
Track `reserve1`/`reserve2` as pallet storage counters updated exclusively inside `do_add_liquidity`/`do_remove_liquidity`/`swap`, instead of reading the live balance of the pool account, so unsolicited transfers to the pool account cannot influence LP-token pricing (mirroring how canonical AMMs cache reserves and only "sync" them through explicit accounting paths). Alternatively/additionally, require a substantially larger locked minimum-liquidity floor tied to pool value (not just a flat `MintMinLiquidity` token count) to make the donation attack economically unviable.

### Proof of Concept
1. Attacker creates a pool (`create_pool`) and adds minimal liquidity via `add_liquidity`, receiving `total_supply = S` LP tokens (`Pools::<T>` entry created, small `S`).
2. Attacker calls `pallet_balances::transfer_keep_alive` / `pallet_assets::transfer` to send a large amount `D` of asset1 and asset2 directly to the pool's sovereign account (computed off-chain from `AccountIdConverter<PalletId, PoolId>`), bypassing `do_add_liquidity` entirely — `reserve1`, `reserve2` in storage-derived-live-balance terms jump by `D`, but `total_supply` (`Pools::<T>` `lp_token` issuance) is unchanged.
3. Victim calls `add_liquidity(asset1, asset2, amount1_desired, amount2_desired, ...)` depositing a fair amount of tokens. `lp_token_amount = min(amount1 * S / reserve1, amount2 * S / reserve2)` is computed against the now-inflated `reserve1`/`reserve2`, so the victim is minted far fewer LP tokens than their contribution is worth relative to the pool's real value.
4. Attacker calls `remove_liquidity`, burning their `S` (or fraction of `S`) LP tokens and receiving a proportional share of the pool's real reserves (`reserve1`, `reserve2`, which now includes both the donation `D` and the victim's deposit), realizing the value transferred from the victim. [3](#0-2)

### Citations

**File:** substrate/frame/asset-conversion/src/lib.rs (L809-814)
```rust
			let pool = Pools::<T>::get(&pool_id).ok_or(Error::<T>::PoolNotFound)?;
			let pool_account =
				T::PoolLocator::address(&pool_id).map_err(|_| Error::<T>::InvalidAssetPair)?;

			let reserve1 = Self::get_balance(&pool_account, asset1.clone());
			let reserve2 = Self::get_balance(&pool_account, asset2.clone());
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L855-892)
```rust
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

			Self::deposit_event(Event::LiquidityAdded {
				who: who.clone(),
				mint_to: mint_to.clone(),
				pool_id,
				amount1_provided: amount1,
				amount2_provided: amount2,
				lp_token: pool.lp_token,
				lp_token_minted: lp_token_amount,
			});

			Ok(lp_token_amount)
		}
```
