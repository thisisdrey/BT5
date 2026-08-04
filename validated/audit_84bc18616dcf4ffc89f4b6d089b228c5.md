## Analysis

The report's core broken invariant is: **a public entrypoint assumes "amount requested to transfer" == "amount actually moved," and uses the unchecked nominal amount for downstream accounting, while the transfer primitive can legitimately return a smaller actual amount.** The `AdvancedOrderEngine.fillOrders()` bug is the fee-on-transfer analog of this general class. The polkadot-sdk repo has a direct structural analog in `pallet-asset-conversion`.

`fungibles::Mutate::transfer` (default implementation) is defined to potentially move **less** than the requested amount: it calls `decrease_balance(..., BestEffort, ...)` and `increase_balance(..., BestEffort)`, and returns the *actual* amount moved as its `Ok` value — the caller is expected to check this return value if exactness matters. [1](#0-0) 

The codebase itself proves this is a known, real hazard: `substrate/frame/asset-conversion/ops/src/lib.rs` explicitly guards every such transfer with `ensure!(balance == T::Assets::transfer(...)?, Error::<T>::PartialTransfer)`. [2](#0-1) 

However, the core AMM logic in `pallet-asset-conversion`'s `do_add_liquidity` and `do_remove_liquidity` (and `credit_swap`) call `T::Assets::transfer(...)` and **discard the returned actual amount**, then compute/mint LP tokens using the nominal requested `amount1`/`amount2` instead of the amount that actually landed in the pool account: [3](#0-2) [4](#0-3) 

### Title
Unchecked Transfer Return Value in `pallet-asset-conversion::do_add_liquidity`/`do_remove_liquidity` Allows Reserve/LP-Share Desync for Fee-Deducting or Partial-Transfer Asset Kinds - (File: `substrate/frame/asset-conversion/src/lib.rs`)

### Summary
`do_add_liquidity` and `do_remove_liquidity` transfer tokens via `T::Assets::transfer(asset, ..., amount, Preserve/Expendable)` but never check the `Ok` return value against the requested `amount`. LP token minting/reserve accounting is computed from the *nominal* `amount1`/`amount2`, not the amount actually credited to/debited from the pool account. Because the underlying `fungibles::Mutate::transfer` default semantics explicitly allow returning less than requested (`BestEffort` precision), any `T::AssetKind` whose `Fungibles` adapter can legitimately move less than requested — analogous to a fee-on-transfer token in the report — desynchronizes pool reserves from LP-token supply, exactly the `balance[before] == balance[after]` invariant break described in the report.

### Finding Description
`T::Assets: Mutate<...>` for `pallet-asset-conversion` is a generic associated type; its `transfer` is only required to satisfy the `fungibles::Mutate` trait contract, whose default `transfer` implementation is defined with `BestEffort` decrease/increase precision and returns the *actually* moved amount, which can be strictly less than the amount requested. [5](#0-4) 

In `do_add_liquidity`, the transfer of `amount1`/`amount2` from the liquidity provider to `pool_account` ignores this return value entirely:
```rust
T::Assets::transfer(asset1, who, &pool_account, amount1, Preserve)?;
T::Assets::transfer(asset2, who, &pool_account, amount2, Preserve)?;
...
lp_token_amount = side1.min(side2); // computed from nominal amount1/amount2
``` [6](#0-5) 

If the actual balance credited to `pool_account` is less than `amount1`/`amount2` (analogous to a fee-on-transfer deduction), the LP token amount minted to `mint_to` is still computed as if the full nominal amount landed in the pool. This mints LP shares backed by fewer real reserve assets than accounted for, diluting all other LP holders' claim on the pool — the exact "vault must not leak funds" invariant break cited in the report, just inverted (LP shares over-minted relative to real reserves rather than vault losing tokens).

The same unchecked pattern recurs in `do_remove_liquidity`'s outbound transfers and in `credit_swap`'s pool-to-pool hop transfer, meaning downstream reserve reads (`get_balance`/`get_reserves`) used for pricing and further swaps can silently diverge from real balances after any transfer that does not move the full nominal amount. [7](#0-6) [4](#0-3) 

Existing guards do not stop this path: `Preservation::Preserve`/`Expendable` only control whether an account is permitted to die or be protected — they do not force `Precision::Exact`, so a partial transfer still returns `Ok(actual)` without an error. The pallet's own sibling crate (`asset-conversion-ops`) demonstrates the maintainers are aware partial transfers are possible and require an explicit `PartialTransfer` check — a check that is missing from the core liquidity/swap paths. [2](#0-1) 

### Impact Explanation
This is a public, unprivileged entrypoint (`add_liquidity`/`remove_liquidity`/swap extrinsics) reachable by any signed account. A desync between minted LP shares and real pool reserves is a value-conservation break in an asset-accounting pallet shipped as part of the Polkadot SDK runtime stack: it can result in LP token holders being unable to redeem their proportional share (fund lock for later withdrawers) or in a depositor being credited LP shares backed by assets never actually received by the pool (unbacked mint of pool value), both of which fall under the "asset accounting must conserve value and settle exactly once" pivot.

### Likelihood Explanation
Exploitability depends on a runtime configuring `T::Assets`/`T::AssetKind` with an asset adapter that can return a partial transfer (any implementation relying on the default `BestEffort` `fungibles::Mutate::transfer`, or a custom adapter modeling externally fee-bearing/deflationary tokens registered as poolable `AssetKind`s, e.g. bridged assets). Given `pallet-asset-conversion` is generic over `AssetKind`/`Assets` specifically to support arbitrary registered fungibles (including foreign/bridged assets), and the sibling `ops` crate already treats partial transfers as a first-class expected failure mode, this is a realistic configuration risk rather than a purely theoretical one.

### Recommendation
In `do_add_liquidity`, `do_remove_liquidity`, and `credit_swap`, capture the `Ok` value returned by every `T::Assets::transfer` call and either (a) `ensure!` it equals the requested amount (mirroring the `PartialTransfer` pattern already used in `asset-conversion-ops`), or (b) use the actual returned amount for all downstream LP-mint/reserve calculations instead of the nominal requested amount.

### Proof of Concept
1. Configure a runtime's `pallet-asset-conversion::Config::Assets` with an `AssetKind`/`Fungibles` adapter whose `transfer` can return `actual < amount` (e.g., relies on the default `BestEffort` implementation, or wraps an asset with any transfer-time deduction).
2. Call `add_liquidity(asset1, asset2, amount1_desired, amount2_desired, ...)` where the adapter transfers less than `amount1_desired` into `pool_account`.
3. Observe that `do_add_liquidity` still computes `lp_token_amount` from the full `amount1_desired`/`amount2_desired` (see `substrate/frame/asset-conversion/src/lib.rs:855-872`) and mints that many LP tokens to `mint_to`.
4. Compare `T::Assets::balance(asset1, &pool_account)` (real reserve) against the LP-token-implied reserve share — they diverge, proving the pool now owes more assets via LP redemption than it actually holds.

### Citations

**File:** substrate/frame/support/src/traits/tokens/fungibles/regular.rs (L362-386)
```rust
	/// Transfer funds from one account into another.
	///
	/// A transfer where the source and destination account are identical is treated as No-OP after
	/// checking the preconditions.
	fn transfer(
		asset: Self::AssetId,
		source: &AccountId,
		dest: &AccountId,
		amount: Self::Balance,
		preservation: Preservation,
	) -> Result<Self::Balance, DispatchError> {
		let _extra = Self::can_withdraw(asset.clone(), source, amount)
			.into_result(preservation != Expendable)?;
		Self::can_deposit(asset.clone(), dest, amount, Extant).into_result()?;
		if source == dest {
			return Ok(amount);
		}

		Self::decrease_balance(asset.clone(), source, amount, BestEffort, preservation, Polite)?;
		// This should never fail as we checked `can_deposit` earlier. But we do a best-effort
		// anyway.
		let _ = Self::increase_balance(asset.clone(), dest, amount, BestEffort);
		Self::done_transfer(asset, source, dest, amount);
		Ok(amount)
	}
```

**File:** substrate/frame/asset-conversion/ops/src/lib.rs (L221-231)
```rust
			ensure!(
				balance2 ==
					T::Assets::transfer(
						asset2.clone(),
						&prior_account,
						&new_account,
						balance2,
						Preservation::Expendable,
					)?,
				Error::<T>::PartialTransfer
			);
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

**File:** substrate/frame/asset-conversion/src/lib.rs (L951-952)
```rust
			T::Assets::transfer(asset1, &pool_account, withdraw_to, amount1, Expendable)?;
			T::Assets::transfer(asset2, &pool_account, withdraw_to, amount2, Expendable)?;
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L1208-1214)
```rust
							T::Assets::transfer(
								asset2.clone(),
								&pool_from,
								&pool_to,
								*amount_out,
								Preserve,
							)?;
```
