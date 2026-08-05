## Finding

The requested local analog is in `pallet-assets`' genesis build implementation.

### Title
Missing genesis consistency check between `assets` and `next_asset_id` can permanently stall public `create` in `pallet-assets` - (File: `substrate/frame/assets/src/lib.rs`)

### Summary
`GenesisConfig::build()` for `pallet-assets` inserts the genesis `assets` list into the `Asset` storage map and, independently, writes `self.next_asset_id` into `NextAssetId` — with no assertion that the two are consistent (i.e., that `next_asset_id` is not equal to, or below, an id already present in the `assets` list). This mirrors exactly the reported bug class: absence of a genesis-time `assert!()` linking related identifier-counter storage items (`CoreAssetId`/`NextAssetId`/`AssetIds` in the external report; `assets`/`NextAssetId` here). [1](#0-0) 

### Finding Description
`GenesisConfig::build()` performs per-item checks (`Asset id already in use`, `Min balance should not be zero`, `Asset does not exist` for metadata) but never validates `self.next_asset_id` against `self.assets`: [2](#0-1) [3](#0-2) 

If a chain operator's genesis config declares an asset with id `N` in `assets` while also setting `next_asset_id` to a value `<= N` (a plausible chart-config mistake, exactly the class of error the external report warns about), the pallet ends up with `Asset::<T,I>::contains_key(N) == true` while `NextAssetId::<T,I>::get() <= N`.

At runtime, `AutoIncAssetId` drives the permissionless `create` extrinsic, which requires the submitted `id` to exactly equal the current `NextAssetId` value before checking for collision: [4](#0-3) 

Because `contains_key` is checked before the `id == next_id` check, once the auto-increment counter walks up to the pre-existing colliding id `N`, every future call to `create` fails with `Error::InUse` and `AssetIdAllocator::advance()` is never invoked (it only runs after a *successful* create) — see the un-conditional post-insert advance path: [5](#0-4) 

`NextAssetId` is now permanently stuck at `N`, and the unprivileged `create` extrinsic can never succeed again for this pallet instance. Only a privileged `ForceOrigin` via `force_create`/`do_force_create` (which does not require matching the allocator) can unstick it: [6](#0-5) 

This is exactly the invariant the external report calls out: no `assert!()` ties the genesis-provided identifier list to the genesis-provided "next id" counter, so a single misconfigured genesis file silently produces an inconsistent, permanently-broken state for a public entry point.

### Impact Explanation
This falls under "public underpriced work that degrades block production or stalls bridge processing" / general "runtime bugs that compromise intended behavior" from the impact gate: the permissionless `Assets::create` call becomes permanently unusable for the affected pallet instance (e.g., asset-hub's trust-backed or pool-assets pallet) as soon as the auto-increment counter reaches the colliding pre-existing id, with no way for unprivileged users to recover — only governance/`ForceOrigin` intervention can. This is a genuine self-inflicted DoS of core asset-creation functionality traceable directly to the missing genesis consistency assertion, not to any malicious/privileged actor's runtime action.

### Likelihood Explanation
Requires a specific genesis misconfiguration (an id in `assets` at or above `next_asset_id`), so it is not exploitable by an attacker post-genesis. Likelihood is moderate: genesis configs for `pallet-assets` are hand-authored/JSON-patched (see `prdoc/stable2412/pr_5687.prdoc` describing manual auto-increment id setup for Asset Hub), and nothing in `assimilate_storage`/`build()` protects against this ordering mistake before it becomes irreversible on-chain. [7](#0-6) 

### Recommendation
In `GenesisConfig::build()`, after inserting `self.assets` and before/when setting `NextAssetId`, add an `assert!()` that `next_asset_id` is strictly greater than every id present in `self.assets` (and not already present in storage), e.g.:
```rust
if let Some(next_asset_id) = &self.next_asset_id {
    assert!(
        self.assets.iter().all(|(id, ..)| id < next_asset_id),
        "next_asset_id must be greater than every genesis asset id"
    );
    NextAssetId::<T, I>::put(next_asset_id);
}
```

### Proof of Concept
1. Build a genesis config for a runtime using `pallet_assets::GenesisConfig` with:
   - `assets: vec![(100, owner, true, 1)]`
   - `next_asset_id: Some(50)`
2. Chain starts; `Asset::<T,I>::contains_key(100) == true`, `NextAssetId::<T,I>::get() == Some(50)`.
3. Unprivileged users repeatedly call `Assets::create` with the sequential ids `50, 51, ... 99`, each succeeding and advancing `NextAssetId`.
4. When `NextAssetId` reaches `100`, any call to `Assets::create(.., id=100, ..)` fails with `Error::<T,I>::InUse` (per [8](#0-7) ), and no other `id` value is accepted (`BadAssetId`).
5. `NextAssetId` is now permanently pinned at `100`; the public `create` extrinsic is permanently disabled for this pallet instance except via governance/`ForceOrigin` `force_create`.

### Citations

**File:** substrate/frame/assets/src/lib.rs (L579-641)
```rust
	#[pallet::genesis_build]
	impl<T: Config<I>, I: 'static> BuildGenesisConfig for GenesisConfig<T, I> {
		fn build(&self) {
			for (id, owner, is_sufficient, min_balance) in &self.assets {
				assert!(!Asset::<T, I>::contains_key(id), "Asset id already in use");
				assert!(!min_balance.is_zero(), "Min balance should not be zero");
				Asset::<T, I>::insert(
					id,
					AssetDetails {
						owner: owner.clone(),
						issuer: owner.clone(),
						admin: owner.clone(),
						freezer: owner.clone(),
						supply: Zero::zero(),
						deposit: Zero::zero(),
						min_balance: *min_balance,
						is_sufficient: *is_sufficient,
						accounts: 0,
						sufficients: 0,
						approvals: 0,
						status: AssetStatus::Live,
					},
				);
			}

			for (id, name, symbol, decimals) in &self.metadata {
				assert!(Asset::<T, I>::contains_key(id), "Asset does not exist");

				let bounded_name: BoundedVec<u8, T::StringLimit> =
					name.clone().try_into().expect("asset name is too long");
				let bounded_symbol: BoundedVec<u8, T::StringLimit> =
					symbol.clone().try_into().expect("asset symbol is too long");

				let metadata = AssetMetadata {
					deposit: Zero::zero(),
					name: bounded_name,
					symbol: bounded_symbol,
					decimals: *decimals,
					is_frozen: false,
				};
				Metadata::<T, I>::insert(id, metadata);
			}

			for (id, account_id, amount) in &self.accounts {
				let result = <Pallet<T, I>>::increase_balance(
					id.clone(),
					account_id,
					*amount,
					|details| -> DispatchResult {
						debug_assert!(
							details.supply.checked_add(&amount).is_some(),
							"checked in prep; qed"
						);
						details.supply = details.supply.saturating_add(*amount);
						Ok(())
					},
				);
				assert!(result.is_ok());
			}

			if let Some(next_asset_id) = &self.next_asset_id {
				NextAssetId::<T, I>::put(next_asset_id);
			}
```

**File:** substrate/frame/assets/src/lib.rs (L843-858)
```rust
		pub fn create(
			origin: OriginFor<T>,
			id: T::AssetIdParameter,
			admin: AccountIdLookupOf<T>,
			min_balance: T::Balance,
		) -> DispatchResult {
			let id: T::AssetId = id.into();
			let owner = T::CreateOrigin::ensure_origin(origin, &id)?;
			let admin = T::Lookup::lookup(admin)?;

			ensure!(!Asset::<T, I>::contains_key(&id), Error::<T, I>::InUse);
			ensure!(!min_balance.is_zero(), Error::<T, I>::MinBalanceZero);

			if let Some(next_id) = T::AssetIdAllocator::next() {
				ensure!(id == next_id, Error::<T, I>::BadAssetId);
			}
```

**File:** substrate/frame/assets/src/lib.rs (L879-881)
```rust
			);
			ensure!(T::CallbackHandle::created(&id, &owner).is_ok(), Error::<T, I>::CallbackFailed);
			T::AssetIdAllocator::advance().map_err(|_| Error::<T, I>::AssetIdAllocationFailed)?;
```

**File:** substrate/frame/assets/src/functions.rs (L760-794)
```rust
	pub(super) fn do_force_create(
		id: T::AssetId,
		owner: T::AccountId,
		is_sufficient: bool,
		min_balance: T::Balance,
		enforce_allocator: bool,
	) -> DispatchResult {
		ensure!(!Asset::<T, I>::contains_key(&id), Error::<T, I>::InUse);
		ensure!(!min_balance.is_zero(), Error::<T, I>::MinBalanceZero);
		if enforce_allocator {
			if let Some(next_id) = T::AssetIdAllocator::next() {
				ensure!(id == next_id, Error::<T, I>::BadAssetId);
			}
		}

		Asset::<T, I>::insert(
			&id,
			AssetDetails {
				owner: owner.clone(),
				issuer: owner.clone(),
				admin: owner.clone(),
				freezer: owner.clone(),
				supply: Zero::zero(),
				deposit: Zero::zero(),
				min_balance,
				is_sufficient,
				accounts: 0,
				sufficients: 0,
				approvals: 0,
				status: AssetStatus::Live,
			},
		);
		ensure!(T::CallbackHandle::created(&id, &owner).is_ok(), Error::<T, I>::CallbackFailed);
		T::AssetIdAllocator::advance_from(&id)
			.map_err(|_| Error::<T, I>::AssetIdAllocationFailed)?;
```

**File:** prdoc/stable2412/pr_5687.prdoc (L8-17)
```text
    description: |
      Setup auto incremented asset id to `50_000_000` for trust backed assets.

      ### Migration
      This change does not break the API but introduces a new constraint. It implements 
      an auto-incremented ID strategy for Trust-Backed Assets (50 pallet instance indexes on both 
      networks), starting at ID 50,000,000. Each new asset must be created with an ID that is one 
      greater than the last asset created. The next ID can be fetched from the `NextAssetId` 
      storage item of the assets pallet. An empty `NextAssetId` storage item indicates no 
      constraint on the next asset ID and can serve as a feature flag for this release.
```
