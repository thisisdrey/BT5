## Finding Summary

Based on my investigation, the closest local analog to the Cally "missing `amount > 0` check → unlimited spam creation" bug is in `pallet-asset-conversion`'s pool-creation path, where the *only* deterrent against unbounded creation of new on-chain entries is a configurable fee that several production runtimes in this repo set to zero.

### Title
Free, unbounded liquidity-pool creation spam due to `PoolSetupFee = 0` and no minimum-value gate in `do_create_pool` - (File: `substrate/frame/asset-conversion/src/lib.rs`)

### Summary
`pallet-asset-conversion::Pallet::create_pool` (dispatched via `do_create_pool`) is a permissionless, signed-origin extrinsic that inserts a new entry into the `Pools` storage map for any distinct `(asset1, asset2)` pair. The only anti-spam control is `T::PoolSetupFee`, withdrawn from the caller in `do_create_pool`. Multiple production runtime configs in this repository set `PoolSetupFee = ConstU128<0>`, with an explicit comment claiming "Asset class deposit fees are sufficient to prevent spam" — an assumption that is not enforced anywhere in `do_create_pool` itself. [1](#0-0) 

### Finding Description
`do_create_pool` only checks that `asset1 != asset2` and that the derived `pool_id` does not already exist; it charges `T::PoolSetupFee::get()` from the creator and then unconditionally creates a new LP-token asset class and inserts a `PoolInfo` record: [2](#0-1) 

Unlike the sibling `pallet-asset-rewards::create_pool`, which explicitly requires `T::Assets::asset_exists(...)` for both legs before creating a pool record, `pallet-asset-conversion::do_create_pool` has no equivalent existence or minimum-value check on the asset pair being registered: [3](#0-2) 

The economic backstop that is supposed to compensate for this — the `PoolSetupFee` — is configured to zero in multiple production runtimes shipped in this repository (`asset-hub-westend`, `asset-hub-rococo`, `penpal`, `staking-async` parachain), all carrying the same comment asserting that *other* pallets' deposit requirements will prevent spam: [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6) 

This mirrors the Cally bug class exactly: a public entrypoint (`createVault` / `create_pool`) that creates a persistent, user-visible on-chain object (`vault` / `PoolInfo` + LP-token asset) with no floor value or existence requirement on the underlying asset, relying entirely on an assumed-but-unenforced external cost. Anyone holding two already-registered, cheap/no-deposit asset identifiers (e.g. `Native` paired with any of the many trust-backed or foreign assets already present on an asset hub, which require no per-pool deposit beyond the zeroed `PoolSetupFee`) can call `create_pool` repeatedly to register arbitrary-looking pools, each minting a real LP-token asset class and emitting a `PoolCreated` event, at no charge.

### Impact Explanation
Each successful call permanently grows the `Pools` and `NextPoolAssetId`-derived asset storage, and emits `PoolCreated`, which is what indexers/front-ends (e.g. wallets, DEX UIs) use to list tradable pools — precisely the "malicious vault" griefing scenario in the seed report, except here it degrades a production DEX pallet used on Asset Hub chains. Because creation is free (`PoolSetupFee = 0`), the attack has no cost ceiling: an attacker can flood the pool listing with junk/duplicate-looking pool entries, degrading UX, bloating chain state, and potentially misleading users into interacting with meaningless pools (e.g. pairing two illiquid or attacker-controlled assets), without any of it constituting "malicious validator/collator/relayer" behavior — it is a plain unprivileged user action.

### Likelihood Explanation
High on any chain that reuses the `PoolSetupFee = ConstU128<0>` configuration shipped in this repo's asset-hub/penpal/staking-async runtimes. The only requirement is that the two `AssetKind` values used are distinct and that the pool doesn't already exist — trivially satisfiable by iterating over any of the many already-registered assets on an asset hub (trust-backed assets, foreign/XCM-registered assets), so no privileged action, governance, or compromised actor is needed.

### Recommendation
Do not rely solely on `PoolSetupFee` for anti-spam. In `do_create_pool` (and `setup_pool_from_genesis` where relevant), add an explicit floor similar to `pallet-asset-rewards::create_pool`'s `ensure!(T::Assets::asset_exists(...), ...)`, and/or enforce `T::PoolSetupFee::get() > Zero::zero()` (or a dedicated non-zero `Consideration`/deposit tied to storage footprint) at the pallet level so runtime configuration cannot silently disable the anti-spam guarantee the code comments assume exists.

### Proof of Concept
1. On a runtime with `PoolSetupFee = ConstU128<0>` (e.g. `asset-hub-westend`, `asset-hub-rococo`, `penpal`, `staking-async` parachain configs cited above).
2. A signed, otherwise-unprivileged account repeatedly calls `AssetConversion::create_pool(origin, Box::new(asset_a), Box::new(asset_b))` for many distinct already-registered `(asset_a, asset_b)` pairs it does not need to own significant value in — see `do_create_pool` at [8](#0-7) .
3. Each call succeeds, withdraws `0` fee, creates a new LP-token asset, and inserts a new `Pools` entry with a `PoolCreated` event — with no check on whether the pool is economically meaningful.
4. Repeating this without bound floods `Pools` storage and any UI/indexer built on `PoolCreated` events, exactly analogous to the flood of zero-value vaults in the Cally report.

Note: I was unable to fully verify, within the available tool budget, whether `T::Assets::touch`/`should_touch` inside `do_create_pool` would reject asset identifiers that are *entirely* unregistered/nonexistent (this could add an incidental partial gate for genuinely-fictitious assets). The finding above is grounded in the confirmed absence of any asset-existence/minimum-value check in `do_create_pool` itself and the confirmed zero `PoolSetupFee` in multiple shipped runtime configs, which is sufficient on its own to allow free, repeated pool creation using assets that already exist on-chain.

### Citations

**File:** substrate/frame/asset-conversion/src/lib.rs (L729-773)
```rust
		pub(crate) fn do_create_pool(
			creator: &T::AccountId,
			asset1: T::AssetKind,
			asset2: T::AssetKind,
			initial_fee: Option<Permill>,
		) -> Result<T::PoolId, DispatchError> {
			ensure!(asset1 != asset2, Error::<T>::InvalidAssetPair);
			if let Some(fee) = initial_fee {
				ensure!(fee <= T::MaxSwapFee::get(), Error::<T>::FeeTooHigh);
			}

			// prepare pool_id
			let pool_id = T::PoolLocator::pool_id(&asset1, &asset2)
				.map_err(|_| Error::<T>::InvalidAssetPair)?;
			ensure!(!Pools::<T>::contains_key(&pool_id), Error::<T>::PoolExists);

			let pool_account =
				T::PoolLocator::address(&pool_id).map_err(|_| Error::<T>::InvalidAssetPair)?;

			// pay the setup fee
			let fee =
				Self::withdraw(T::PoolSetupFeeAsset::get(), creator, T::PoolSetupFee::get(), true)?;
			T::PoolSetupFeeTarget::on_unbalanced(fee);

			if T::Assets::should_touch(asset1.clone(), &pool_account) {
				T::Assets::touch(asset1.clone(), &pool_account, creator)?
			};

			if T::Assets::should_touch(asset2.clone(), &pool_account) {
				T::Assets::touch(asset2.clone(), &pool_account, creator)?
			};

			let lp_token = NextPoolAssetId::<T>::get()
				.or(T::PoolAssetId::initial_value())
				.ok_or(Error::<T>::IncorrectPoolAssetId)?;
			let next_lp_token_id = lp_token.increment().ok_or(Error::<T>::IncorrectPoolAssetId)?;
			NextPoolAssetId::<T>::set(Some(next_lp_token_id));

			T::PoolAssets::create(lp_token.clone(), pool_account.clone(), false, 1u32.into())?;
			if T::PoolAssets::should_touch(lp_token.clone(), &pool_account) {
				T::PoolAssets::touch(lp_token.clone(), &pool_account, creator)?
			};

			let pool_info = PoolInfo { lp_token: lp_token.clone() };
			Pools::<T>::insert(pool_id.clone(), pool_info);
```

**File:** substrate/frame/asset-rewards/src/lib.rs (L843-854)
```rust
	fn create_pool(
		creator: &T::AccountId,
		staked_asset_id: T::AssetId,
		reward_asset_id: T::AssetId,
		reward_rate_per_block: T::Balance,
		expiry: DispatchTime<BlockNumberFor<T>>,
		admin: &T::AccountId,
	) -> Result<PoolId, DispatchError> {
		// Ensure the assets exist.
		ensure!(T::Assets::asset_exists(staked_asset_id.clone()), Error::<T>::NonExistentAsset);
		ensure!(T::Assets::asset_exists(reward_asset_id.clone()), Error::<T>::NonExistentAsset);

```

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/src/lib.rs (L500-504)
```rust
	type PoolAssetId = u32;
	type PoolAssets = PoolAssets;
	type PoolSetupFee = ConstU128<0>; // Asset class deposit fees are sufficient to prevent spam
	type PoolSetupFeeAsset = WestendLocation;
	type PoolSetupFeeTarget = ResolveAssetTo<AssetConversionOrigin, Self::Assets>;
```

**File:** cumulus/parachains/runtimes/assets/asset-hub-rococo/src/lib.rs (L447-451)
```rust
	type PoolAssetId = u32;
	type PoolAssets = PoolAssets;
	type PoolSetupFee = ConstU128<0>; // Asset class deposit fees are sufficient to prevent spam
	type PoolSetupFeeAsset = TokenLocation;
	type PoolSetupFeeTarget = ResolveAssetTo<AssetConversionOrigin, Self::Assets>;
```

**File:** cumulus/parachains/runtimes/testing/penpal/src/lib.rs (L571-575)
```rust
	type PoolAssetId = u32;
	type PoolAssets = PoolAssets;
	type PoolSetupFee = ConstU128<0>; // Asset class deposit fees are sufficient to prevent spam
	type PoolSetupFeeAsset = xcm_config::PenpalNativeCurrency;
	type PoolSetupFeeTarget = ResolveAssetTo<AssetConversionOrigin, Self::Assets>;
```

**File:** substrate/frame/staking-async/runtimes/parachain/src/lib.rs (L442-446)
```rust
	type PoolAssetId = u32;
	type PoolAssets = PoolAssets;
	type PoolSetupFee = ConstU128<0>; // Asset class deposit fees are sufficient to prevent spam
	type PoolSetupFeeAsset = WestendLocation;
	type PoolSetupFeeTarget = ResolveAssetTo<AssetConversionOrigin, Self::Assets>;
```
