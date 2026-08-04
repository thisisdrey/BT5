## Title
Unregistered asset-conversion pools can be swapped against because `credit_swap()` / `get_reserves()` never check the `Pools` registry - (`File: substrate/frame/asset-conversion/src/lib.rs`)

## Summary
`pallet-asset-conversion`'s swap execution path derives a deterministic pool account from an asset pair via `T::PoolLocator::pool_address()` and immediately transfers/withdraws against that address, without ever checking that a `PoolInfo` for that pair actually exists in the `Pools` storage map. This mirrors the Symbiotic report's root cause exactly: a caller-supplied/derivable identifier (`vault` there, an asset pair here) is used to source funds/compute state without validating it is a registered entity, so any account balance sitting at the derived-but-unregistered address is silently treated as legitimate pool liquidity.

## Finding Description
`Pools::<T>` is the single source of truth for "has a pool actually been created" — the pallet's own doc comment says so explicitly: [1](#0-0) 

However, the core swap routine `credit_swap()` (called by both `swap_exact_tokens_for_tokens` and `swap_tokens_for_exact_tokens`) never queries `Pools::<T>::get(pool_id)`. It only derives the deterministic pool account: [2](#0-1) 

and then withdraws/transfers straight from/to that address: [3](#0-2) 

`get_reserves()` — used both by `credit_swap`'s callers (via `balance_path_from_amount_out`, `get_amount_out`/`get_amount_in`) and by the public `quote_price_*` view functions — also never checks `Pools::<T>::get`; it only verifies the derived account's balances are non-zero: [4](#0-3) 

`pool_address()`/`pool_id()` from `T::PoolLocator` are pure deterministic functions of the asset pair — they succeed for *any* valid asset-pair encoding, registered or not. Because `PalletId`-style sub-accounts are derivable off-chain by anyone, an attacker can:
1. Compute the pool address for an arbitrary, never-created (or since-destroyed) asset pair.
2. Transfer/mint tokens of `asset1` and `asset2` directly into that address (this is exactly the scenario the doc comment on `Pools` warns about — "rather than people sending tokens directly to a pool's public account").
3. Call `swap_exact_tokens_for_tokens` / `swap_tokens_for_exact_tokens` with that unregistered pair in the `path`.
4. `validate_swap_path()` only checks path length and pool-pair uniqueness — it does not check `Pools::<T>::contains_key`, so the swap proceeds against attacker-funded balances at the unregistered "pool" account, using the constant-product AMM formula on manipulated reserves that were never subject to `create_pool`'s deposit/fee/LP-token accounting.

This is the direct analog of the Symbiotic `getOperatorVotingPower`/`getOperatorVotingPowerAt` bug: the vulnerable functions validate a *derived resource* (collateral token / balances) but never validate that the underlying registry entry (`vault` / `Pools` pool) is actually registered, letting an attacker substitute an unofficial, self-funded stand-in for a registered entity.

## Impact Explanation
Swaps executed against a self-funded, unregistered "pool" account bypass all of `create_pool`'s protections (pool creation deposit, `PoolSetupFee`, canonical LP-token minting/burn accounting, `PoolFees` configuration). An attacker fully controls both sides of the "reserves" they seeded, so they can construct arbitrary price ratios for a swap path, extracting more of the real asset (e.g. `asset2`) than they deposit, or use it as a laundering/dust-manipulation vector against any downstream consumers of `quote_price_exact_tokens_for_tokens`/`quote_price_tokens_for_exact_tokens` (both used by wallets/other pallets to price conversions) since those also route through `get_reserves()` without a `Pools` existence check. This falls squarely under "runtime bugs that compromise intended behavior" / "public underpriced work" for balances/asset accounting, satisfying the Polkadot SDK impact gate.

## Likelihood Explanation
No privileged actor is required. Deriving a `PalletId` sub-account from a known formula and asset ids is a fully public, deterministic computation (`T::PoolLocator::pool_address`), and funding an account with tokens the attacker already owns is a normal signed transaction. The swap extrinsics (`swap_exact_tokens_for_tokens`, `swap_tokens_for_exact_tokens`) are plain `ensure_signed` public dispatchables with no additional origin filter beyond signing. The only gating check present (`validate_swap_path`) verifies pair uniqueness and length, not pool registration, so the path is unobstructed.

## Recommendation
In `credit_swap()`'s `resolve_path` closure (and in `get_reserves()`), after computing `pool_from`/`pool_to`/`pool_account` via `PoolLocator`, add an explicit `Pools::<T>::get(&pool_id).ok_or(Error::<T>::PoolNotFound)?` (or `contains_key`) check before touching balances, mirroring the check already done in `pool_fee_for` indirectly via `pool_id` but not enforced against the `Pools` map. This ensures swaps and quotes only ever operate on pools that went through `create_pool`'s deposit/registration flow.

## Proof of Concept
1. Choose `AssetA`, `AssetB` that have never had `create_pool(AssetA, AssetB)` called (or call `create_pool` then later have it torn down/never officially exist for this pair).
2. Compute `pool_address = T::PoolLocator::pool_address(AssetA, AssetB)` (deterministic, computable off-chain).
3. As attacker, mint/transfer a skewed ratio of `AssetA`/`AssetB` directly into `pool_address` (e.g., huge `AssetB`, tiny `AssetA`).
4. Call `swap_exact_tokens_for_tokens(path=[AssetA, AssetB], amount_in=small, amount_out_min=0, send_to=attacker, keep_alive=false)`.
5. Because `credit_swap`/`get_reserves` never verify `Pools::<T>::get((AssetA,AssetB))`, the swap executes against the attacker-seeded balances and returns an amount of `AssetB` determined purely by the attacker's chosen (fake) reserves — extracting value with no real counterparty liquidity or pool accounting involved.

### Citations

**File:** substrate/frame/asset-conversion/src/lib.rs (L213-217)
```rust
	/// Map from `PoolAssetId` to `PoolInfo`. This establishes whether a pool has been officially
	/// created rather than people sending tokens directly to a pool's public account.
	#[pallet::storage]
	pub type Pools<T: Config> =
		StorageMap<_, Blake2_128Concat, T::PoolId, PoolInfo<T::PoolAssetId>, OptionQuery>;
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L1194-1222)
```rust
		fn credit_swap(
			credit_in: CreditOf<T>,
			path: &BalancePath<T>,
		) -> Result<CreditOf<T>, (CreditOf<T>, DispatchError)> {
			let resolve_path = || -> Result<CreditOf<T>, DispatchError> {
				for pos in 0..=path.len() {
					if let Some([(asset1, _), (asset2, amount_out)]) = path.get(pos..=pos + 1) {
						let pool_from = T::PoolLocator::pool_address(asset1, asset2)
							.map_err(|_| Error::<T>::InvalidAssetPair)?;

						if let Some((asset3, _)) = path.get(pos + 2) {
							let pool_to = T::PoolLocator::pool_address(asset2, asset3)
								.map_err(|_| Error::<T>::InvalidAssetPair)?;

							T::Assets::transfer(
								asset2.clone(),
								&pool_from,
								&pool_to,
								*amount_out,
								Preserve,
							)?;
						} else {
							let credit_out =
								Self::withdraw(asset2.clone(), &pool_from, *amount_out, true)?;
							return Ok(credit_out);
						}
					}
				}
				Err(Error::<T>::InvalidPath.into())
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L1225-1243)
```rust
			let credit_out = match resolve_path() {
				Ok(c) => c,
				Err(e) => return Err((credit_in, e)),
			};

			let pool_to = if let Some([(asset1, _), (asset2, _)]) = path.get(0..2) {
				match T::PoolLocator::pool_address(asset1, asset2) {
					Ok(address) => address,
					Err(_) => return Err((credit_in, Error::<T>::InvalidAssetPair.into())),
				}
			} else {
				return Err((credit_in, Error::<T>::InvalidPath.into()));
			};

			T::Assets::resolve(&pool_to, credit_in)
				.map_err(|c| (c, Error::<T>::BelowMinimum.into()))?;

			Ok(credit_out)
		}
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
