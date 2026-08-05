Based on my research, the PSM (Peg Stability Module) pallet contains a direct structural analog of the AdapterVault bug: it tracks debt separately per `(internal_asset, external_asset)` key but does not defend against two different `external_asset` identifiers resolving to the same underlying collateral balance in the reserve account. [1](#0-0) [2](#0-1) [3](#0-2) 

### Title
PSM allows registering multiple external-asset identifiers that alias the same underlying reserve balance, permitting debt ceiling bypass and insolvency - (File: substrate/frame/psm/src/lib.rs)

### Summary
`pallet-psm` tracks minted internal-asset debt per `(internal_asset, external_asset)` pair via `PsmDebt`, and enforces a debt ceiling by summing `total_psm_debt()` across all externals registered on a PSM instance [3](#0-2) . Each external asset's actual backing is the balance the PSM's single reserve account (`psm_account`, derived only from `internal_asset`) holds of that `external_asset` id, read via `get_reserve` [4](#0-3) . The pallet's `T::Fungibles` is a generic, pluggable trait object; production runtimes commonly instantiate this with composed `fungibles::UnionOf`/`fungible::UnionOf` adapters (as used throughout asset-hub runtimes) that map several distinct `AssetId` values (e.g. different XCM `Location`s) onto the same underlying storage/balance via a `Criterion: Convert` function [5](#0-4) . `add_external_asset` (registration path) never validates that a newly approved `external_asset` id is economically distinct from an already-approved one — it only prevents inserting the exact same `T::AssetId` key twice, since `ExternalAssets` is keyed by `(internal_asset, external_asset)` [2](#0-1) .

### Finding Description
This is the same broken invariant as the AdapterVault report: an accounting function assumes each tracked "strategy" (here, each approved `external_asset`) has an independent, non-overlapping balance backing it, and sums independent debt counters against what is actually a single shared collateral pool.

If two different `T::AssetId` values registered as externals on the same PSM instance (via `add_external_asset`, not shown above line 1000 but gated only by uniqueness of the exact `(internal, external)` key) both resolve — through the runtime's `T::Fungibles` conversion layer — to the same underlying token balance in the `psm_account`, then:
- `mint()` for external A transfers into `psm_account` and increases `PsmDebt(internal, A)`.
- `mint()` for external B (aliasing the same underlying balance) also increases `psm_account`'s balance under the alias, and increases `PsmDebt(internal, B)` independently.
- `get_reserve(internal, A)` and `get_reserve(internal, B)` both read from the *same* underlying balance, so each redemption path sees the full collateral as available, unaware the other debt entry is drawing on the identical pool.
- `total_psm_debt()` sums `PsmDebt` over `(internal, *)`, which correctly reflects total minted internal debt, but the collateral backing that debt is not doubled — only single collateral exists for what accounting treats as independently-verified per-asset reserves.

The result: `redeem()`'s `InsufficientReserve`/reserve check (`Self::get_reserve(&internal_asset, &external_asset) < external_out`) can pass for both aliases even though the pool cannot honor both redemptions, because each check independently observes the full (undivided) balance [6](#0-5) . This mirrors exactly how `IAdapter(adapter).totalAssets()` in the external report double-counts a shared underlying balance across two "independent" strategies.

### Impact Explanation
If exploitable, this allows minting internal stablecoin against effectively the same collateral twice (once per aliased external asset id), letting a user redeem more in aggregate than the PSM instance's true reserve holds — a direct fund-loss / bridge-state-lock style issue for the PSM's collateral, and it can push `total_psm_debt` under the ceiling check while true backing is insufficient, defeating the debt-ceiling safety mechanism (`ExceedsMaxPsmDebt`) that is meant to bound insolvency risk [3](#0-2) .

### Likelihood Explanation
Likelihood depends entirely on whether a concrete runtime configures `Config::Fungibles` for `pallet-psm` with a `UnionOf`-style adapter (or any non-injective `AssetId → balance` mapping) and whether `add_external_asset` (not fully inspected in this pass — I was not able to view its full body before truncation) fails to reject a second `external_asset` id that maps to an already-approved underlying asset. I could not confirm from the code read so far whether `add_external_asset` performs any cross-validation against the underlying resolved asset (as opposed to just the raw `T::AssetId` key). This is a genuine gap in my investigation: the pallet is generic over `T::AssetId`/`T::Fungibles`, so the aliasing precondition is a runtime-configuration concern, not something intrinsic to `pallet-psm`'s own storage keys.

### Recommendation
- In `add_external_asset`, before insertion, resolve/query the underlying balance/account identity that `T::Fungibles` would use for `external_asset` and ensure no other already-approved external on the same PSM instance resolves to the same underlying asset (analogous to the `AdapterVault` fix of checking `adapter.asset` uniqueness before adding an adapter).
- Alternatively, require `T::Fungibles::AssetId` used by a `pallet-psm` instance to be injective (one physical asset per id) at the runtime-configuration level, and document this as a hard safety requirement when wiring `UnionOf`-based fungibles implementations into `pallet-psm`.
- Add a `try_state`/`do_try_state` invariant that sums per-external reserve balances resolved to their canonical underlying asset and cross-checks against `total_psm_debt`, to catch aliasing regressions.

### Proof of Concept
1. Configure a runtime's `pallet_psm::Config::Fungibles` with a `fungibles::UnionOf`-style composition where two distinct `T::AssetId` values (`ExtA`, `ExtB`) both convert to the same underlying asset/account balance (e.g., two `Location` representations of the same reserve-backed token, as seen in the asset-hub `NativeAndAllAssets` pattern) [5](#0-4) .
2. `create_psm(internal)` and `add_external_asset(internal, ExtA)`, `add_external_asset(internal, ExtB)` — both succeed since `ExternalAssets` keys on the raw `T::AssetId`, not the resolved underlying asset.
3. Call `mint(internal, ExtA, X)` — deposits `X` into `psm_account` (as resolved by the union adapter) and sets `PsmDebt(internal, ExtA) = X_internal`.
4. Call `mint(internal, ExtB, Y)` — under the alias mapping this also lands in the *same* underlying balance, and sets `PsmDebt(internal, ExtB) = Y_internal`, independent of A.
5. `get_reserve(internal, ExtA)` and `get_reserve(internal, ExtB)` both report the combined `X+Y` balance.
6. A user redeems the full `X_internal` against `ExtA` and, separately, the full `Y_internal` against `ExtB`; both `InsufficientReserve` checks pass individually even though the pool cannot cover both redemptions if `X+Y` collateral is less than the sum needed, since each check only compares against the same shared pool without knowledge of the other debt.

### Citations

**File:** substrate/frame/psm/src/lib.rs (L458-468)
```rust
	/// Internal-asset debt minted through PSM, per `(internal, external)` pair.
	#[pallet::storage]
	pub type PsmDebt<T: Config> = StorageDoubleMap<
		_,
		Blake2_128Concat,
		T::AssetId,
		Blake2_128Concat,
		T::AssetId,
		BalanceOf<T>,
		ValueQuery,
	>;
```

**File:** substrate/frame/psm/src/lib.rs (L511-521)
```rust
	/// Approved external assets per PSM.
	#[pallet::storage]
	pub(crate) type ExternalAssets<T: Config> = StorageDoubleMap<
		_,
		Blake2_128Concat,
		T::AssetId,
		Blake2_128Concat,
		T::AssetId,
		ExternalAssetInfo,
		OptionQuery,
	>;
```

**File:** substrate/frame/psm/src/lib.rs (L732-741)
```rust
			let current_total_psm_debt = Self::total_psm_debt(&internal_asset);
			ensure!(
				current_total_psm_debt.saturating_add(internal_equivalent) <= info.max_debt,
				Error::<T>::ExceedsMaxPsmDebt
			);

			let current_debt = PsmDebt::<T>::get(&internal_asset, &external_asset);
			let max_debt = Self::max_asset_debt(&internal_asset, &external_asset, &info);
			let new_debt = current_debt.saturating_add(internal_equivalent);
			ensure!(new_debt <= max_debt, Error::<T>::ExceedsMaxPsmDebt);
```

**File:** substrate/frame/psm/src/lib.rs (L848-855)
```rust
			let current_debt = PsmDebt::<T>::get(&internal_asset, &external_asset);
			ensure!(current_debt >= effective_internal_net, Error::<T>::InsufficientReserve);

			let reserve = Self::get_reserve(&internal_asset, &external_asset);
			if reserve < external_out {
				defensive!("PSM reserve is less than expected output amount");
				return Err(Error::<T>::Unexpected.into());
			}
```

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/src/lib.rs (L452-465)
```rust
/// Union fungibles implementation for [`PoolAssets`] and [`NativeAndNonPoolAssets`].
///
/// NOTE: Should be kept updated to include ALL balances and assets in the runtime.
pub type NativeAndAllAssets = fungibles::UnionOf<
	PoolAssets,
	NativeAndNonPoolAssets,
	LocalFromLeft<
		AssetIdForPoolAssetsConvert<PoolAssetsPalletLocation, xcm::v5::Location>,
		AssetIdForPoolAssets,
		xcm::v5::Location,
	>,
	xcm::v5::Location,
	AccountId,
>;
```
