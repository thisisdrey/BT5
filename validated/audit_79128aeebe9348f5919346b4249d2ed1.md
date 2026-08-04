### Title
`add_external_asset` never initializes `AssetCeilingWeight`, permanently bricking minting for every newly approved external asset - (File: `substrate/frame/psm/src/lib.rs`)

### Summary
This is the same bug class as the Bond Protocol M-7 finding: a state variable that gates a core accounting calculation (`createFeeDiscount` there, `AssetCeilingWeight` here) has no code path that initializes it to a usable value when the associated entity is created, and the only way to set it is a separate, easy-to-forget extrinsic. Because the storage defaults to zero and zero is defined to mean "disabled," the feature is silently broken until an admin remembers to call the setter — exactly mirroring the "discount is always zero because nothing ever writes it" pattern from the report.

### Finding Description
`AssetCeilingWeight` is declared as a plain `ValueQuery` double map with **no custom default**, so it resolves to `Permill::zero()` for any `(internal_asset, external_asset)` pair that has never been explicitly set: [1](#0-0) 

The pallet's own documentation states the semantics of a zero weight explicitly: *"Zero disables minting for that external."* This value feeds `max_asset_debt`, which computes the per-asset mint ceiling via `normalised_ceiling`: [2](#0-1) 

`normalised_ceiling` returns `BalanceOf::<T>::zero()` whenever `weight == 0`. This zero ceiling is then enforced in `mint`: [3](#0-2) 

`add_external_asset` — the only extrinsic that approves a new external asset for a PSM — writes `ExternalAssets` and bumps `PsmInfo::external_count`, but at no point inserts an entry into `AssetCeilingWeight`: [4](#0-3) 

The only way to give an external asset a non-zero (i.e. usable) ceiling weight is the separate `set_asset_ceiling_weight` extrinsic: [5](#0-4) 

Nothing in `add_external_asset`, `create_psm`, or the pallet's genesis/migration logic forces this second call to happen. If the `full_admin` follows the documented flow of `create_psm` → `add_external_asset` → users start minting (exactly like the pallet doc's worked "Example" in the module-level docs, which calls `mint`/`redeem` immediately after fee lookups with no mention of ceiling weight), every `mint` call for that external will unconditionally fail `ExceedsMaxPsmDebt` because `max_asset_debt` resolves to `0` and `new_debt (>0) <= max_debt (0)` is always false. `redeem` is unaffected because it only checks `PsmDebt`/reserve, not `AssetCeilingWeight`, so once debt exists, redemption still works — but no debt can ever be minted in the first place.

### Impact Explanation
This is a runtime bug that compromises the pallet's intended behavior: the core purpose of the PSM (letting users mint the internal stablecoin against an approved external asset) is silently and completely disabled for any freshly approved external asset, with no error message pointing at the real cause (`ExceedsMaxPsmDebt` looks like a debt-ceiling-reached condition, not a "you forgot a setup step" condition). Any runtime that wires up this pallet and follows the natural creation flow (`create_psm`, then `add_external_asset`) ends up with a PSM that silently accepts no mints for that asset until an operator notices and manually calls `set_asset_ceiling_weight` — mirroring the report's "the create fee discount feature is broken... there is no way to initialize it" pattern almost exactly, just for a ceiling-weight rather than a fee-discount value.

### Likelihood Explanation
High: this triggers on the very first, ordinary use of the pallet's documented workflow, with no adversarial input needed. Any deployment of `pallet-psm` will hit it unless the runtime integrator or governance operator is specifically aware of the extra, undocumented `set_asset_ceiling_weight` call requirement — the same "administrators aren't told they must call a separate setter" failure mode that made the original Bond report a valid finding.

### Recommendation
Have `add_external_asset` also initialize `AssetCeilingWeight` to a sane non-zero default (e.g. `Permill::one()` if it's the only or first external, or an equal share among existing externals recomputed on each addition), or change `max_asset_debt`/`normalised_ceiling` so that an external asset with no explicit weight entry falls back to the full `PsmInfo::max_debt` (analogous to how `MintingFee`/`RedemptionFee` correctly use a `DefaultFee` `Get` implementation instead of relying on `ValueQuery`'s implicit zero). At minimum, document the required extra call prominently and add an integration/benchmark test that calls `mint` immediately after `add_external_asset` without `set_asset_ceiling_weight`, which would have caught this.

### Proof of Concept
1. `Root`/asset owner calls `create_psm(internal_asset, full_admin, emergency_admin, fee_destination, max_debt = 1_000_000, min_swap_amount = 1)`.
2. `full_admin` calls `add_external_asset(internal_asset, USDC)` — succeeds, `ExternalAssets` now contains `(internal_asset, USDC)`, but `AssetCeilingWeight::get(internal_asset, USDC)` is still `Permill::zero()` (never written).
3. A user calls `mint(internal_asset, USDC, external_amount = 1000, max_fee)`.
4. Inside `mint`: `Self::max_asset_debt(...)` → `AssetCeilingWeight::get` returns `0` → `normalised_ceiling` returns `0` → `new_debt (>0) <= max_debt (0)` is `false` → the call reverts with `Error::ExceedsMaxPsmDebt`, even though `PsmInfo::max_debt` is `1_000_000` and the PSM has ample capacity.
5. Minting for this external stays impossible for every user until `full_admin` separately calls `set_asset_ceiling_weight(internal_asset, USDC, weight)` — a step not enforced or defaulted anywhere in the pallet.

### Citations

**File:** substrate/frame/psm/src/lib.rs (L498-509)
```rust
	/// Per-external ceiling weight within a PSM, normalised against the sum of weights
	/// for the same instance. Zero disables minting for that external.
	#[pallet::storage]
	pub(crate) type AssetCeilingWeight<T: Config> = StorageDoubleMap<
		_,
		Blake2_128Concat,
		T::AssetId,
		Blake2_128Concat,
		T::AssetId,
		Permill,
		ValueQuery,
	>;
```

**File:** substrate/frame/psm/src/lib.rs (L738-741)
```rust
			let current_debt = PsmDebt::<T>::get(&internal_asset, &external_asset);
			let max_debt = Self::max_asset_debt(&internal_asset, &external_asset, &info);
			let new_debt = current_debt.saturating_add(internal_equivalent);
			ensure!(new_debt <= max_debt, Error::<T>::ExceedsMaxPsmDebt);
```

**File:** substrate/frame/psm/src/lib.rs (L1258-1282)
```rust
		#[pallet::call_index(8)]
		#[pallet::weight(T::WeightInfo::set_asset_ceiling_weight())]
		pub fn set_asset_ceiling_weight(
			origin: OriginFor<T>,
			internal_asset: T::AssetId,
			external_asset: T::AssetId,
			weight: Permill,
		) -> DispatchResult {
			Self::ensure_psm_admin(origin, &internal_asset, |l| l.can_set_asset_ceiling())?;
			ensure!(
				ExternalAssets::<T>::contains_key(&internal_asset, &external_asset),
				Error::<T>::AssetNotApproved
			);
			// Reweighting renormalises every external's ceiling; an external left below its new
			// ceiling simply can't be minted until redemptions bring its debt back down.
			let old_value = AssetCeilingWeight::<T>::get(&internal_asset, &external_asset);
			AssetCeilingWeight::<T>::insert(&internal_asset, &external_asset, weight);
			Self::deposit_event(Event::AssetCeilingWeightUpdated {
				internal_asset,
				external_asset,
				old_value,
				new_value: weight,
			});
			Ok(())
		}
```

**File:** substrate/frame/psm/src/lib.rs (L1343-1356)
```rust
			ExternalAssets::<T>::insert(
				&internal_asset,
				&external_asset,
				ExternalAssetInfo {
					status: CircuitBreakerLevel::AllEnabled,
					decimals: asset_decimals,
				},
			);
			info.external_count = info.external_count.saturating_add(1);
			Psm::<T>::insert(&internal_asset, info);

			Self::deposit_event(Event::ExternalAssetAdded { internal_asset, external_asset });
			Ok(())
		}
```

**File:** substrate/frame/psm/src/lib.rs (L1538-1550)
```rust
		/// A single external's normalised debt ceiling: its share of the total weight applied
		/// to `max_debt`. Zero if the external (or the PSM as a whole) carries no weight.
		fn normalised_ceiling(
			asset_weight: Permill,
			total_weight: u32,
			max_debt: BalanceOf<T>,
		) -> BalanceOf<T> {
			let weight = asset_weight.deconstruct();
			if weight == 0 || total_weight == 0 {
				return BalanceOf::<T>::zero();
			}
			Perbill::from_rational(weight, total_weight).mul_floor(max_debt)
		}
```
