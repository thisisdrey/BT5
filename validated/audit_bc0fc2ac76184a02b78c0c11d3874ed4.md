[1](#0-0) [2](#0-1)

### Citations

**File:** substrate/frame/psm/src/lib.rs (L1385-1410)
```rust
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

**File:** substrate/frame/psm/src/tests.rs (L1262-1308)
```rust
	#[test]
	fn remove_external_asset_fails_has_debt() {
		ExtBuilder::default().mints(ALICE, 1000 * INTERNAL_UNIT).build_and_execute(|| {
			assert_noop!(
				Psm::remove_external_asset(RuntimeOrigin::root(), INTERNAL_ASSET_ID, USDC_ASSET_ID),
				Error::<Test>::AssetHasDebt
			);
		});
	}

	#[test]
	fn remove_external_asset_succeeds_after_debt_drained() {
		new_test_ext().execute_with(|| {
			// Zero fees so a single mint/redeem pair brings debt exactly to 0.
			set_minting_fee(USDC_ASSET_ID, Permill::zero());
			set_redemption_fee(USDC_ASSET_ID, Permill::zero());

			// With non-zero debt, removal is blocked.
			assert_ok!(Psm::mint(
				RuntimeOrigin::signed(ALICE),
				INTERNAL_ASSET_ID,
				USDC_ASSET_ID,
				1000 * INTERNAL_UNIT,
				Permill::zero()
			));
			assert_noop!(
				Psm::remove_external_asset(RuntimeOrigin::root(), INTERNAL_ASSET_ID, USDC_ASSET_ID),
				Error::<Test>::AssetHasDebt
			);

			// Drain debt to zero — removal now succeeds.
			assert_ok!(Psm::redeem(
				RuntimeOrigin::signed(ALICE),
				INTERNAL_ASSET_ID,
				USDC_ASSET_ID,
				1000 * INTERNAL_UNIT,
				Permill::zero()
			));
			assert_eq!(PsmDebt::<Test>::get(INTERNAL_ASSET_ID, USDC_ASSET_ID), 0);
			assert_ok!(Psm::remove_external_asset(
				RuntimeOrigin::root(),
				INTERNAL_ASSET_ID,
				USDC_ASSET_ID
			));
			assert!(!ExternalAssets::<Test>::contains_key(INTERNAL_ASSET_ID, USDC_ASSET_ID));
		});
	}
```
