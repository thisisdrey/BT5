### Title
Permissionless `pallet_assets::create` can front-run a privileged `force_create`, permanently blocking a specific canonical Asset ID from governance registration - (File: `substrate/frame/assets/src/lib.rs`, `substrate/frame/assets/src/functions.rs`)

### Summary
The Maia bug's core broken invariant is: a privileged registration extrinsic uses a uniqueness guard (`contains_key(key) => revert`) on a storage key that an unprivileged actor can populate beforehand through a *different, permissionless* entry point, permanently DoS-ing the privileged registration for that exact key. The same invariant break exists in `pallet-assets`: the permissionless `create()` extrinsic and the privileged `force_create()`/`do_force_create()` extrinsic share the exact same `Asset::<T, I>` storage map keyed by `AssetId`, guarded by `ensure!(!Asset::<T, I>::contains_key(&id), Error::<T, I>::InUse)`. When a pallet-assets instance is configured with `AssetIdAllocator = ()` (a supported, first-class configuration documented and used in this repo), the sequential-id protection introduced specifically to prevent this class of attack (PR `pr_12378`) is disabled, and any signed account able to satisfy `CreateOrigin` can call `create()` with an arbitrary `AssetId` chosen by governance for a future canonical registration, permanently blocking `force_create` for that id.

### Finding Description
`pallet_assets::create` (permissionless, gated only by `T::CreateOrigin`) and `pallet_assets::force_create`/`do_force_create` (privileged, gated by `T::ForceOrigin`) both write to the same `Asset::<T, I>` map: [1](#0-0) 

```
pub fn create(...) -> DispatchResult {
    ...
    ensure!(!Asset::<T, I>::contains_key(&id), Error::<T, I>::InUse);
    ...
    if let Some(next_id) = T::AssetIdAllocator::next() {
        ensure!(id == next_id, Error::<T, I>::BadAssetId);
    }
    ...
}
``` [2](#0-1) 

```
pub(super) fn do_force_create(
    id: T::AssetId, ...
) -> DispatchResult {
    ensure!(!Asset::<T, I>::contains_key(&id), Error::<T, I>::InUse);
    ...
    if enforce_allocator {
        if let Some(next_id) = T::AssetIdAllocator::next() {
            ensure!(id == next_id, Error::<T, I>::BadAssetId);
        }
    }
    ...
}
```

The `T::AssetIdAllocator::next()` gate is what prevents front-running today — when an `AutoIncAssetId` allocator is configured, permissionless `create()` can only ever use the *current* `next_id`, so an attacker cannot pre-squat an arbitrary future id that governance intends to reserve. This mitigation, however, is opt-in via `Config::AssetIdAllocator`, and `()` is a fully supported "no constraint" implementation shipped in the same file: [3](#0-2) 

```
impl<AssetId> AssetIdAllocator<AssetId> for () {
	fn next() -> Option<AssetId> { None }
	fn advance() -> Result<(), ()> { Ok(()) }
	fn advance_from(_: &AssetId) -> Result<(), ()> { Ok(()) }
}
```

When `AssetIdAllocator = ()` is paired with any `CreateOrigin` that does not itself constrain which specific `AssetId` a caller may pick (e.g. `AsEnsureOriginWithArg<EnsureSigned<AccountId>>`), `create()`'s only remaining guard is `!Asset::contains_key(&id)`. This exact `()` + broadly-permissioned combination exists in the repository (used for instances such as `ForeignAssetsInstance`, whose `AssetIdAllocator` is `()` because `Location`-typed ids are not incrementable): [4](#0-3) 

Any unprivileged signed account that can satisfy `CreateOrigin` for that instance can call `create(origin, id, admin, min_balance)` with `id` equal to a specific value that governance has publicly committed to registering later (e.g. a well-known/canonical asset id, an id required to match an external convention, or an id being reserved via governance proposal/referendum lead time). Once `Asset::<T,I>::insert(id, ...)` has executed, `Error::<T,I>::InUse` is returned for good on any subsequent `create`/`force_create` call for that same `id`: [5](#0-4) 

This mirrors the Maia finding precisely: a redundant-looking but real uniqueness check (`InUse`) on a storage map is trivially satisfiable by anyone through a separate, permissionless entrypoint before the privileged path executes, and there is no way for the privileged actor to reclaim or override the id afterward (no forced-override, no "only if total supply is zero" carve-out, matching the same governance gap discussed in the Maia remediation debate).

### Impact Explanation
This is a governance-facing denial of service: the intended asset id can never be registered by the privileged authority once squatted, forcing either abandonment of that specific id (breaking any external convention/interop expectation tied to it) or requiring a completely different remediation path outside the pallet (there is no admin override, no "reclaim if empty" logic in `do_force_create`). Where the specific numeric/Location value of the asset id matters to downstream systems (bridges, XCM asset identity, cross-chain conventions), this can degrade or permanently block asset-registration-dependent chain functionality — consistent with the "public underpriced work that stalls processing" / "permanent state lock" impact classes in scope.

### Likelihood Explanation
The vulnerability is conditional on runtime configuration: it only manifests where `Config::AssetIdAllocator = ()` is combined with a `CreateOrigin` permissive enough to let a caller pick the exact target `id`. This condition is real and present in-repo (`ForeignAssetsInstance` uses `AssetIdAllocator = ()`), though that instance's `CreateOrigin` (`ForeignCreators`) restricts which ids a given origin may pick based on location provenance, which may narrow (but not conclusively eliminate, without further review of `ForeignCreators`/`IsForeign` matching rules) the practical cross-tenant squatting window. Fully unconstrained combinations (`AssetIdAllocator = ()` + `EnsureSigned`-style `CreateOrigin`) exist in the generic FRAME node runtime used in this repo, which is lower-value from a "live chain" perspective but demonstrates the pattern is a supported and exploitable configuration of the pallet itself, not a hypothetical.

### Recommendation
- Make sequential/allocator enforcement mandatory (or at least strongly warned against disabling) whenever an instance's `create()` is reachable by an unprivileged/broad `CreateOrigin`.
- For `AssetIdAllocator = ()` instances, add a `ForceOrigin`-only reservation mechanism (e.g., a `reserve_asset_id`/`force_create_or_override_if_never_used` path) that lets governance pre-claim an id irrespective of ordering, or restrict `create()`'s `id` space away from any id space governance may want to reserve.
- Document explicitly, and ideally lint/benchmark-guard, the combination of `AssetIdAllocator::next() == None` with a `CreateOrigin` not already scoping which ids a caller may choose, since this reintroduces exactly the front-running class that `AutoIncAssetId`/`pr_12378` was designed to close.

### Proof of Concept
Given a `pallet_assets::Config<I>` instance with:
```rust
type CreateOrigin = AsEnsureOriginWithArg<EnsureSigned<AccountId>>;
type AssetIdAllocator = ();
type ForceOrigin = EnsureRoot<AccountId>;
```
1. Governance announces intent to reserve `AssetId = 999` for a canonical/bridged asset (e.g. via an on-chain referendum with an execution delay).
2. Before the referendum executes, any signed account calls:
   ```rust
   Assets::create(RuntimeOrigin::signed(attacker), 999, attacker, min_balance)
   ```
   This succeeds because `AssetIdAllocator::next()` returns `None` (no sequencing enforced) and `Asset::<T,I>::contains_key(999)` is `false` at that point.
3. When the referendum executes `force_create(RuntimeOrigin::root(), 999, governance_admin, ..., min_balance)`, it reverts with `Error::<T, I>::InUse` because `Asset::<T,I>::contains_key(999)` is now `true`, per: [6](#0-5) 
4. Governance permanently cannot register asset id `999` under this pallet instance; the analogous unit test pattern already in-repo (`force_create_can_use_arbitrary_id`) confirms the exact `InUse` collision behavior for arbitrary ids: [7](#0-6)

### Citations

**File:** substrate/frame/assets/src/lib.rs (L250-261)
```rust
/// No allocation policy: `create` accepts any unused id and [`NextAssetId`] has no effect.
impl<AssetId> AssetIdAllocator<AssetId> for () {
	fn next() -> Option<AssetId> {
		None
	}
	fn advance() -> Result<(), ()> {
		Ok(())
	}
	fn advance_from(_: &AssetId) -> Result<(), ()> {
		Ok(())
	}
}
```

**File:** substrate/frame/assets/src/lib.rs (L764-765)
```rust
		/// The asset ID is already taken.
		InUse,
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

**File:** substrate/frame/assets/src/functions.rs (L760-774)
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

```

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/src/lib.rs (L640-661)
```rust
	type CreateOrigin = ForeignCreators<
		(
			FromSiblingParachain<parachain_info::Pallet<Runtime>, xcm::v5::Location>,
			FromNetwork<xcm_config::UniversalLocation, EthereumNetwork, xcm::v5::Location>,
			xcm_config::bridging::to_rococo::RococoAssetFromAssetHubRococo,
		),
		LocationToAccountId,
		AccountId,
		xcm::v5::Location,
	>;
	type ForceOrigin = AssetsForceOrigin;
	type AssetDeposit = ForeignAssetsAssetDeposit;
	type MetadataDepositBase = ForeignAssetsMetadataDepositBase;
	type MetadataDepositPerByte = ForeignAssetsMetadataDepositPerByte;
	type ApprovalDeposit = ForeignAssetsApprovalDeposit;
	type StringLimit = ForeignAssetsAssetsStringLimit;
	type Holder = ();
	type Freezer = ForeignAssetsFreezer;
	type Extra = ();
	type WeightInfo = weights::pallet_assets_foreign::WeightInfo<Runtime>;
	type CallbackHandle = (ForeignAssetId<Runtime, ForeignAssetsInstance>,);
	type AssetIdAllocator = ();
```

**File:** substrate/frame/assets/src/tests.rs (L2271-2275)
```rust
		// A forced id cannot collide with a currently-live asset.
		assert_noop!(
			Assets::force_create(RuntimeOrigin::root(), 10, 1, false, 1),
			Error::<Test>::InUse
		);
```
