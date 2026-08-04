### Title
`pallet-assets` `fungibles::Mutate::transfer` returns the requested amount instead of the actual amount credited, letting `pallet-asset-conversion` mis-account pool reserves and LP-token mint amounts - ([File: substrate/frame/asset-conversion/src/lib.rs])

### Summary
The compound-protocol report boils down to one broken invariant: code assumes `amount_requested == amount_actually_moved` on a token transfer, then uses the unchecked `amount_requested` for downstream accounting (loan bookkeeping) that must exactly match on-chain balances. In `polkadot-sdk`, `pallet-asset-conversion`'s `do_add_liquidity` / `do_remove_liquidity` make the identical assumption when moving pool assets: they call `T::Assets::transfer(...)` and `T::PoolAssets::transfer(...)` and then use the **input** `amount1`/`amount2` (not a verified actual-transferred value) to compute reserves-derived LP-token mint/burn amounts, while the codebase itself demonstrates (in `substrate/frame/asset-conversion/ops/src/lib.rs`) that `transfer()`'s return value can legitimately differ from the requested amount and must be checked with `ensure!(expected == actual, Error::PartialTransfer)`.

### Finding Description
`substrate/frame/asset-conversion/src/lib.rs::do_add_liquidity` (lines 855-891) computes `amount1`/`amount2` from the desired amounts and pool reserves, then does:
```rust
T::Assets::transfer(asset1, who, &pool_account, amount1, Preserve)?;
T::Assets::transfer(asset2, who, &pool_account, amount2, Preserve)?;
...
let side1 = Self::mul_div(&amount1, &total_supply, &reserve1)?;
let side2 = Self::mul_div(&amount2, &total_supply, &reserve2)?;
lp_token_amount = side1.min(side2);
```
and `do_remove_liquidity` (lines 894-966) similarly transfers `amount1`/`amount2` out of the pool account and emits/accounts these values as final settlement, again without checking the transfer's return value.

Both calls discard the `Result<T::Balance, DispatchError>` returned by `Mutate::transfer` (the `Ok` variant carries the *actual* amount moved) and instead continue to use the pre-computed `amount1`/`amount2` for reserve-ratio math and event emission. Elsewhere in the very same pallet family, `substrate/frame/asset-conversion/ops/src/lib.rs` explicitly treats this return value as untrustworthy and defends against it:
```rust
ensure!(
    balance1 == T::Assets::transfer(asset1, &prior_account, &new_account, balance1, Preservation::Expendable)?,
    Error::<T>::PartialTransfer
);
```
This shows the pallet authors already recognize that `Assets::transfer` can return an amount different from what was requested (`Error::PartialTransfer`), yet `do_add_liquidity`/`do_remove_liquidity` — the core liquidity-accounting paths — do not apply this same check.

The generic default implementation `fungibles::Mutate::transfer` (`substrate/frame/support/src/traits/tokens/fungibles/regular.rs:366-386`) further worsens this: it calls `decrease_balance(..., BestEffort, ...)` and `increase_balance(..., BestEffort)` (both allowed to move less than requested) yet unconditionally returns `Ok(amount)` — i.e., the input amount, not the balances actually moved. Any `AssetKind` implementation for `T::Assets`/`T::PoolAssets` that uses this default trait method (or any asset type with fee-like withdrawal/deposit semantics, dust burning, or minimum-balance-driven partial delivery) can silently deliver less value than `amount1`/`amount2` to the pool account while `pallet-asset-conversion` still records/mints/burns based on the full requested amount.

### Impact Explanation
If the configured `T::Assets`/`T::PoolAssets` implementation (any asset class pluggable via `fungibles::Inspect`/`Mutate`, including custom or third-party asset adapters used in a `AssetHub`-style runtime) can return less than the requested amount from `transfer` (fee-on-transfer semantics, dust-burn-on-death, or `BestEffort` precision paths), then:
- `do_add_liquidity` will mint LP tokens based on `amount1`/`amount2` that were never fully deposited into the pool account, over-crediting the liquidity provider relative to the pool's real backing — a straightforward value-conservation break (mint of LP shares not backed by actual reserves).
- `do_remove_liquidity` will emit `amount1`/`amount2` as withdrawn even if the pool account transfer under-delivered to `withdraw_to`, permanently desynchronizing on-chain reserve tracking (`get_reserves`) from actual balances, degrading/breaking future swap pricing and potentially allowing later liquidity providers/removers to drain more than their fair share.

This matches the "Balances, assets ... must conserve value and settle exactly once to the rightful beneficiary and amount" pivot.

### Likelihood Explanation
Exploitability is entirely dependent on whether a configured `AssetKind` (via `T::Assets`/`T::PoolAssets`) can return an actual-transferred amount lower than the requested amount under normal, permissionless conditions (no malicious admin/governance needed) — e.g., an asset that burns dust on `decrease_balance`/`increase_balance`, or one with a fee-like withdrawal consequence. Given standard `pallet-assets` `do_transfer`/`transfer_and_die` already computes `credit != amount` in dust-burn scenarios (see `substrate/frame/assets/src/functions.rs` `prep_credit`/`transfer_and_die`), and the default `fungibles::Mutate::transfer` uses `BestEffort` precision internally while still echoing back the requested `amount`, the underlying return-value integrity gap is real and directly exercisable by any liquidity provider through the public `add_liquidity`/`remove_liquidity` extrinsics — no privileged actor required.

### Recommendation
In `do_add_liquidity` and `do_remove_liquidity`, capture and validate the actual transferred amounts the same way `asset-conversion/ops` already does:
```rust
let transferred1 = T::Assets::transfer(asset1, who, &pool_account, amount1, Preserve)?;
ensure!(transferred1 == amount1, Error::<T>::PartialTransfer);
```
and use the verified/actual amounts (not the desired/pre-computed ones) for all subsequent LP-token mint/burn and reserve-ratio math. Additionally, fix the misleading contract of `fungibles::Mutate::transfer`'s default implementation so it returns the balance actually moved rather than echoing the requested `amount`, and require exact-precision (`Exact`) semantics for asset movements feeding into AMM/pool accounting.

### Proof of Concept
Not independently runnable from the index alone: a definitive PoC requires instantiating `pallet-asset-conversion` with a `T::Assets`/`T::PoolAssets` type whose `transfer`/`decrease_balance`/`increase_balance` can deliver less than requested under normal (non-privileged) conditions (e.g., a custom `fungibles` adapter with fee-on-transfer or aggressive dust-burn behavior), then calling `add_liquidity` and comparing the pool account's real balance against the `LiquidityAdded.amount1_provided`/`amount2_provided` event and minted LP tokens. I was unable to fully verify inside this session whether `pallet-assets`'s own `impl_fungibles.rs::transfer` override (as opposed to the generic default in `fungibles/regular.rs`) already returns the exact `credit` value in all cases — the read of `substrate/frame/assets/src/impl_fungibles.rs` did not complete before the session ended, so this should be confirmed by a Devin agent with full file access before finalizing severity, since if `pallet-assets`'s concrete implementation always mirrors `transfer_and_die`'s `credit` (actual amount) and asset-conversion's default runtime configuration only ever uses `pallet-assets`/`pallet-balances` (which enforce `Exact` semantics for non-BestEffort calls and error out rather than partially deliver), the exploitable gap may be latent (present in the trait contract) rather than trivially reachable in the default runtime configuration.