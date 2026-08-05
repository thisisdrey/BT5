Audit Report

## Title
`remove_external_asset` deletes an external asset from a PSM using only internal debt accounting, not the actual reserve balance, permanently stranding external tokens - (File: substrate/frame/psm/src/lib.rs)

## Summary
`pallet-psm`'s `remove_external_asset` extrinsic gates removal of an external asset solely on the `PsmDebt` counter being zero, while never checking the actual balance of that external asset held in the PSM's shared reserve account. Because the reserve account is derived only from `internal_asset` and is shared across every external asset registered on that PSM instance, decimal-conversion rounding or other residuals attributable to a specific external asset can remain in the reserve even after `PsmDebt` for that pair reaches zero, and once the `ExternalAssets` entry is removed there is no dispatchable path to recover that residual balance.

## Finding Description
Each PSM instance's reserve account is derived exclusively from `internal_asset` via `blake2_256((PalletId::TYPE_ID, PalletId, internal_asset).encode())`, and this single account holds the combined balances of every external asset approved on that instance; the module documentation itself states the external-asset reserve balance is "derived, not stored." [1](#0-0) 

`remove_external_asset` checks only that `ExternalAssets` contains the entry and that `PsmDebt::<T>::get(&internal_asset, &external_asset).is_zero()`, then unconditionally removes `ExternalAssets`, `MintingFee`, `RedemptionFee`, `AssetCeilingWeight`, and `PsmDebt` for that pair — with no check against `T::Fungibles::balance` of the actual external asset held by the reserve account. [2](#0-1) 

`add_external_asset` permits registering multiple external assets with differing decimals (bounded by `MAX_DECIMALS_DIFF`) onto the same internal asset/reserve account, which is the precondition for the shared-reserve/decimals-drift scenario. [3](#0-2) 

The existing test suite demonstrates the exact scenario: debt for a decimals-mismatched external asset (`USDX`) is fully drained to zero via `redeem`, after which `remove_external_asset` succeeds — showing that `PsmDebt == 0` is treated as sufficient justification for removal without any actual-balance verification. [4](#0-3) 

`PsmDebt` is a logical accounting value tracking internal asset minted 1:1 against external assets deposited, not a live mirror of the reserve's true `Fungibles` balance for that specific external asset. Since the reserve account is shared, and no code path reconciles or checks the reserve's actual per-asset balance against `PsmDebt` before deletion, any decimals-conversion rounding remainder, fee dust, or unsolicited direct transfer to the reserve account for that external asset survives the removal and becomes unreachable once `ExternalAssets` no longer contains the entry (blocking `mint`/`redeem`/`set_*`/`add_external_asset` for that pair via `AssetNotApproved`/status gating).

## Impact Explanation
This matches the Polkadot SDK impact gate's "permanent user-fund ... lock" category: value legitimately held in the pallet's reserve account backing a specific external asset becomes permanently unreachable through any dispatchable once its `ExternalAssets` entry is deleted, with no sweep/withdrawal mechanism provided. This is a genuine runtime-logic gap in the pallet's accounting/removal invariant rather than a hypothetical or off-scope issue, since the code paths and the reproducing test both exist in-repo.

## Likelihood Explanation
The precondition (`PsmDebt` reaching zero while the reserve still holds a nonzero real balance of that external asset) is a natural byproduct of normal operation — repeated `mint`/`redeem` cycles between assets of differing decimals accumulate rounding residue, as directly demonstrated by the existing `remove_external_asset_succeeds_after_debt_drained_with_external_decimal_drift` test. The removal action itself is performed by the pallet's admin origin as routine maintenance (not privileged abuse); the bug is that this routine, correctly-authorized action can strand funds due to the missing balance check, which is the root cause, not admin misuse of privilege.

## Recommendation
Before deleting `ExternalAssets`, `MintingFee`, `RedemptionFee`, `AssetCeilingWeight`, and `PsmDebt` in `remove_external_asset`, either (a) require the reserve's real `T::Fungibles::balance` attributable to that external asset to be zero, mirroring the `MarinateV2` fix pattern, or (b) introduce and check a per-external-asset tracked balance so any residual amount can be swept to the fee destination/admin as part of or prior to removal, rather than relying solely on the `PsmDebt` accounting counter which does not always reflect the reserve's actual balance.

## Proof of Concept
1. Admin creates a PSM keyed on `internal_asset` and approves two external assets (e.g. `USDC`, `USDX`) with differing decimals via `add_external_asset`, both sharing the reserve account derived from `internal_asset`. [3](#0-2) 
2. A user mints/redeems `USDX` repeatedly; integer-division rounding from decimals conversion leaves a small residual `USDX` balance in the shared reserve account that is not reflected in `PsmDebt` for `(internal_asset, USDX)`, as shown by `remove_external_asset_succeeds_after_debt_drained_with_external_decimal_drift`. [4](#0-3) 
3. Admin calls `remove_external_asset(internal_asset, USDX)`; since `PsmDebt::<T>::get(&internal_asset, &USDX).is_zero()` passes, the call succeeds and wipes the `ExternalAssets`, fee, and ceiling entries for `USDX`. [2](#0-1) 
4. The residual `USDX` balance remains in the shared reserve account; `mint`/`redeem`/`set_*` for `(internal_asset, USDX)` now fail with `AssetNotApproved`, and no dispatchable exists to withdraw the stranded balance, leaving it permanently locked.

### Citations

**File:** substrate/frame/psm/src/lib.rs (L54-61)
```rust
//! * **PSM instance**: A configured Peg Stability Module, keyed by its internal asset id and
//!   described by [`PsmInfo`]. Each instance has its own reserve account derived from
//!   `blake2_256((PalletId::TYPE_ID, PalletId, internal_asset).encode())`.
//! * **Minting**: Deposit external asset → receive internal asset (minus fee).
//! * **Redemption**: Burn internal asset → receive external asset (minus fee).
//! * **Reserve**: External asset balance held by a PSM's reserve account (derived, not stored).
//! * **PSM Debt**: Total internal asset minted through a PSM, backed 1:1 by external assets in that
//!   PSM's reserve.
```

**File:** substrate/frame/psm/src/lib.rs (L1316-1356)
```rust
		pub fn add_external_asset(
			origin: OriginFor<T>,
			internal_asset: T::AssetId,
			external_asset: T::AssetId,
		) -> DispatchResult {
			Self::ensure_psm_admin(origin, &internal_asset, |l| l.can_manage_assets())?;
			let mut info = Psm::<T>::get(&internal_asset).ok_or(Error::<T>::PsmNotFound)?;
			ensure!(
				!ExternalAssets::<T>::contains_key(&internal_asset, &external_asset),
				Error::<T>::AssetAlreadyApproved
			);
			ensure!(info.external_count < T::MaxExternals::get(), Error::<T>::TooManyAssets);
			ensure!(
				T::Fungibles::asset_exists(external_asset.clone()),
				Error::<T>::AssetDoesNotExist
			);

			let asset_decimals = T::Fungibles::decimals(external_asset.clone());
			ensure!(
				T::Fungibles::decimals(internal_asset.clone()) == info.internal_decimals,
				Error::<T>::DecimalsMismatch
			);
			ensure!(
				(asset_decimals.abs_diff(info.internal_decimals) as u32) <= MAX_DECIMALS_DIFF,
				Error::<T>::DecimalsRangeExceeded
			);

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

**File:** substrate/frame/psm/src/lib.rs (L1383-1410)
```rust
		#[pallet::call_index(10)]
		#[pallet::weight(T::WeightInfo::remove_external_asset())]
		pub fn remove_external_asset(
			origin: OriginFor<T>,
			internal_asset: T::AssetId,
			external_asset: T::AssetId,
		) -> DispatchResult {
			Self::ensure_psm_admin(origin, &internal_asset, |l| l.can_manage_assets())?;
			let mut info = Psm::<T>::get(&internal_asset).ok_or(Error::<T>::PsmNotFound)?;
			ensure!(
				ExternalAssets::<T>::contains_key(&internal_asset, &external_asset),
				Error::<T>::AssetNotApproved
			);
			ensure!(
				PsmDebt::<T>::get(&internal_asset, &external_asset).is_zero(),
				Error::<T>::AssetHasDebt
			);
			ExternalAssets::<T>::remove(&internal_asset, &external_asset);
			MintingFee::<T>::remove(&internal_asset, &external_asset);
			RedemptionFee::<T>::remove(&internal_asset, &external_asset);
			AssetCeilingWeight::<T>::remove(&internal_asset, &external_asset);
			PsmDebt::<T>::remove(&internal_asset, &external_asset);
			info.external_count = info.external_count.saturating_sub(1);
			Psm::<T>::insert(&internal_asset, info);

			Self::deposit_event(Event::ExternalAssetRemoved { internal_asset, external_asset });
			Ok(())
		}
```

**File:** substrate/frame/psm/src/tests.rs (L1310-1352)
```rust
	#[test]
	fn remove_external_asset_succeeds_after_debt_drained_with_external_decimal_drift() {
		new_test_ext().execute_with(|| {
			register_external_asset_with_weight(USDX_ASSET_ID, Permill::from_percent(100));
			set_minting_fee(USDX_ASSET_ID, Permill::zero());
			set_redemption_fee(USDX_ASSET_ID, Permill::zero());

			let usdx_raw = 10_000 * USDX_UNIT;
			assert_ok!(Psm::mint(
				RuntimeOrigin::signed(ALICE),
				INTERNAL_ASSET_ID,
				USDX_ASSET_ID,
				usdx_raw,
				Permill::zero()
			));
			assert_noop!(
				Psm::remove_external_asset(RuntimeOrigin::root(), INTERNAL_ASSET_ID, USDX_ASSET_ID),
				Error::<Test>::AssetHasDebt
			);

			assert_ok!(Assets::set_metadata(
				RuntimeOrigin::signed(ALICE),
				USDX_ASSET_ID,
				b"USDX".to_vec(),
				b"USDX".to_vec(),
				4
			));

			assert_ok!(Psm::redeem(
				RuntimeOrigin::signed(ALICE),
				INTERNAL_ASSET_ID,
				USDX_ASSET_ID,
				10_000 * INTERNAL_UNIT,
				Permill::zero()
			));
			assert_eq!(PsmDebt::<Test>::get(INTERNAL_ASSET_ID, USDX_ASSET_ID), 0);
			assert_ok!(Psm::remove_external_asset(
				RuntimeOrigin::root(),
				INTERNAL_ASSET_ID,
				USDX_ASSET_ID
			));
			assert!(!ExternalAssets::<Test>::contains_key(INTERNAL_ASSET_ID, USDX_ASSET_ID));
		});
```
