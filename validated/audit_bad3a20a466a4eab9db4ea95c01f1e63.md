### Title
`redeem` uses a frozen decimals snapshot even after live asset decimals drift, causing wrong-amount settlement / permanent value mismatch — analog of Chainlink oracle stale-data DoS - (File: `substrate/frame/psm/src/lib.rs`)

### Summary
The external report describes a system that trusts a single external price source (Chainlink), and when that source returns stale/wrong data or goes offline, critical operations either revert (DoS) or silently execute against stale data, causing loss of funds or requiring manual guardian intervention. `pallet-psm` has a structurally identical pattern around its `decimals` snapshot, which plays the same role as an "oracle value" for scaling conversions between the internal stablecoin and each external asset: `ExternalAssetInfo::decimals` and `PsmInfo::internal_decimals` are captured once at registration and used forever afterward as the conversion "price" between units.

### Finding Description
`add_external_asset` snapshots the live decimals of both the internal and external asset at registration time [1](#0-0) . `mint` re-validates this snapshot against current live metadata via `ensure_decimals_match` and halts with `DecimalsMismatch` if it drifted [2](#0-1) , matching the "guardian must reset / stops working until fixed" pattern from the report.

However, `redeem` deliberately **skips this freshness check** and always uses the frozen snapshot values `external.decimals` / `info.internal_decimals` to compute the conversion factor, per the doc comment: "Redemptions use the decimals snapshotted when the PSM/external pair was registered, allowing existing positions to unwind even if live metadata later changes" [3](#0-2) . The actual implementation confirms no `ensure_decimals_match` call inside `redeem` — it reads `external.decimals`/`info.internal_decimals` directly from storage and feeds them into `internal_to_external` [4](#0-3) .

The conversion functions scale amounts strictly by powers of ten derived from the decimals difference [5](#0-4) . If the external asset's live decimals metadata changes after registration (asset owners can freely call `Assets::set_metadata` to alter `decimals`, as demonstrated in the repo's own tests), the snapshot becomes "stale price data" analogous to the Chainlink feed returning wrong data — but instead of reverting (safe failure) or having a fallback, `redeem` continues to execute real token transfers computed from the **wrong** scaling factor. This is confirmed directly by the test `redeem_uses_snapshot_when_asset_decimals_drift`, which mints then changes `USDX` decimals from 2 to 4, and shows `redeem` still succeeds and moves tokens using the old (now-incorrect) decimal assumption [6](#0-5) .

### Impact Explanation
Because the smallest-unit granularity of the external asset changed (e.g., 2 → 4 decimals means what used to be "1 unit" is now 100x smaller), but the PSM still applies the old scale factor, real token amounts transferred out of/into the reserve during `redeem` no longer correspond to the true economic value burned from the user's internal asset balance. This can result in the PSM reserve paying out systematically wrong amounts of the external asset relative to the internal asset burned, breaking the 1:1 backing invariant the whole pallet is designed to guarantee (`PsmDebt` tracked against reserve, verified in `do_try_state`) [7](#0-6) . Depending on the direction of drift, this either drains the reserve faster than debt is retired (fund loss to remaining internal-asset holders/protocol insolvency) or under-pays redeemers (fund lock for users), matching the report's "system becomes uncollateralized" / "users losing collateral" impact categories, without requiring any privileged/root actor to trigger it — only the external asset's own (non-PSM) metadata owner needs to change decimals, which is a normal, permissionless-by-design action for asset owners on many deployments.

### Likelihood Explanation
Likelihood is Low-to-Medium: it requires an external asset whose metadata owner is distinct from PSM governance and who changes decimals after PSM approval — a plausible but not everyday occurrence (e.g., a token contract migration, decimal-precision fix, or an asset admin acting independently of the PSM's own governance). Unlike governance/root abuse, the metadata owner of the *external* asset is typically not the PSM's controlling authority, so this does not require a privileged PSM actor — it only requires an unrelated third party (the asset's own admin) to exercise standard, expected asset-admin functionality.

### Recommendation
Either (a) apply the same `ensure_decimals_match` freshness check in `redeem` as in `mint`, halting redemptions on drift and requiring an explicit governance-driven re-snapshot/migration path (analogous to a "fallback oracle" or authorized re-sync), or (b) if allowing existing positions to unwind is intentional, add an explicit re-scaling/migration mechanism so that debt and reserve amounts are normalized to the new decimals atomically rather than continuing to apply a stale scale factor indefinitely, and add a `do_try_state`/on-chain invariant check that reserve-vs-debt accounting has not drifted due to metadata changes.

### Proof of Concept
Existing repository test demonstrates the exact mechanism (no exploitation is needed beyond what the test shows is already possible): [6](#0-5) 
1. `register_external_asset_with_weight(USDX_ASSET_ID, ...)` snapshots `USDX` decimals = 2.
2. `Psm::mint` succeeds, creating debt/reserve using the 2-decimal scale.
3. Asset owner ALICE calls `Assets::set_metadata(..., decimals=4)`, changing USDX's live decimals to 4 — no PSM admin is involved.
4. `Psm::redeem` is called and **succeeds**, using the frozen 2-decimal snapshot instead of the new live 4-decimal value, producing a transfer amount computed against the wrong scale (test asserts fixed amounts under the *old* scale, which no longer reflect the *true* current external-asset unit value). [2](#0-1)

### Citations

**File:** substrate/frame/psm/src/lib.rs (L778-784)
```rust
		/// to the instance's [`PsmInfo::fee_destination`]), then transfers the resulting
		/// amount in `external_asset` from the PSM reserve to the caller. The fee is
		/// calculated using ceiling rounding (`mul_ceil`), ensuring the protocol never
		/// undercharges. Redemptions use the decimals snapshotted when the PSM/external pair
		/// was registered, allowing existing positions to unwind even if live metadata later
		/// changes.
		///
```

**File:** substrate/frame/psm/src/lib.rs (L811-846)
```rust
		pub fn redeem(
			origin: OriginFor<T>,
			internal_asset: T::AssetId,
			external_asset: T::AssetId,
			internal_amount: BalanceOf<T>,
			max_fee: Permill,
		) -> DispatchResult {
			let who = ensure_signed(origin)?;
			let info = Psm::<T>::get(&internal_asset).ok_or(Error::<T>::PsmNotFound)?;

			let external = ExternalAssets::<T>::get(&internal_asset, &external_asset)
				.ok_or(Error::<T>::UnsupportedAsset)?;
			ensure!(external.status.allows_redemption(), Error::<T>::AllSwapsStopped);

			let ext_decimals = external.decimals;
			let internal_decimals = info.internal_decimals;

			ensure!(internal_amount >= info.min_swap_amount, Error::<T>::BelowMinimumSwap);

			let fee_rate = RedemptionFee::<T>::get(&internal_asset, &external_asset);
			ensure!(fee_rate <= max_fee, Error::<T>::FeeTooHigh);
			let fee = fee_rate.mul_ceil(internal_amount);
			let internal_net = internal_amount.saturating_sub(fee);

			let external_out =
				Self::internal_to_external(internal_net, ext_decimals, internal_decimals)?;
			ensure!(
				internal_net.is_zero() || !external_out.is_zero(),
				Error::<T>::AmountTooSmallAfterConversion
			);
			// `effective_internal_net` is the internal value that round-trips to `external_out`;
			// it is what we actually burn and what the tracked debt decreases by. Any truncation
			// dust stays in the caller's internal balance, symmetric with `mint`, which takes
			// only the round-tripped share of the external amount.
			let effective_internal_net =
				Self::external_to_internal(external_out, ext_decimals, internal_decimals)?;
```

**File:** substrate/frame/psm/src/lib.rs (L1333-1350)
```rust
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
```

**File:** substrate/frame/psm/src/lib.rs (L1601-1624)
```rust
		/// Convert an amount denominated in internal units into external-asset units.
		///
		/// Inverse of [`Self::external_to_internal`]. Floor-divides when internal has more
		/// decimals, multiplies up when it has fewer.
		pub(crate) fn internal_to_external(
			amount: BalanceOf<T>,
			ext_decimals: u8,
			internal_decimals: u8,
		) -> Result<BalanceOf<T>, Error<T>> {
			use core::cmp::Ordering::*;
			match ext_decimals.cmp(&internal_decimals) {
				Equal => Ok(amount),
				Less => {
					let diff = (internal_decimals - ext_decimals) as u32;
					let factor = Self::pow10(diff)?;
					Ok(amount.checked_div(&factor).unwrap_or_else(BalanceOf::<T>::zero))
				},
				Greater => {
					let diff = (ext_decimals - internal_decimals) as u32;
					let factor = Self::pow10(diff)?;
					amount.checked_mul(&factor).ok_or(Error::<T>::ConversionOverflow)
				},
			}
		}
```

**File:** substrate/frame/psm/src/lib.rs (L1636-1651)
```rust
		pub(crate) fn ensure_decimals_match(
			info: &PsmInfo<T>,
			internal_asset: &T::AssetId,
			external_asset: &T::AssetId,
			external: &ExternalAssetInfo,
		) -> Result<(u8, u8), DispatchError> {
			ensure!(
				T::Fungibles::decimals(external_asset.clone()) == external.decimals,
				Error::<T>::DecimalsMismatch
			);
			ensure!(
				T::Fungibles::decimals(internal_asset.clone()) == info.internal_decimals,
				Error::<T>::DecimalsMismatch
			);
			Ok((external.decimals, info.internal_decimals))
		}
```

**File:** substrate/frame/psm/src/lib.rs (L1693-1702)
```rust
					// 1. Per-external reserve covers tracked debt.
					let debt = PsmDebt::<T>::get(&internal_asset, &external_asset);
					let reserve = Self::get_reserve(&internal_asset, &external_asset);
					let debt_as_external =
						Self::internal_to_external(debt, external.decimals, info.internal_decimals)
							.map_err(|_| "Failed to convert tracked debt to external units")?;
					ensure!(
						reserve >= debt_as_external,
						"PSM reserve is less than tracked debt for an asset"
					);
```

**File:** substrate/frame/psm/src/tests.rs (L2981-3028)
```rust
	#[test]
	fn redeem_uses_snapshot_when_asset_decimals_drift() {
		new_test_ext().execute_with(|| {
			register_external_asset_with_weight(USDX_ASSET_ID, Permill::from_percent(100));
			set_zero_fees(USDX_ASSET_ID);

			// Mint first, then change decimals.
			assert_ok!(Psm::mint(
				RuntimeOrigin::signed(ALICE),
				INTERNAL_ASSET_ID,
				USDX_ASSET_ID,
				10_000 * USDX_UNIT,
				Permill::zero()
			));
			assert_ok!(Assets::set_metadata(
				RuntimeOrigin::signed(ALICE),
				USDX_ASSET_ID,
				b"USDX".to_vec(),
				b"USDX".to_vec(),
				4
			));

			let alice_usdx_before = get_asset_balance(USDX_ASSET_ID, ALICE);
			let alice_internal_before = get_asset_balance(INTERNAL_ASSET_ID, ALICE);
			let debt_before = PsmDebt::<Test>::get(INTERNAL_ASSET_ID, USDX_ASSET_ID);

			assert_ok!(Psm::redeem(
				RuntimeOrigin::signed(ALICE),
				INTERNAL_ASSET_ID,
				USDX_ASSET_ID,
				100 * INTERNAL_UNIT,
				Permill::zero()
			));

			assert_eq!(
				get_asset_balance(USDX_ASSET_ID, ALICE),
				alice_usdx_before + 100 * USDX_UNIT
			);
			assert_eq!(
				get_asset_balance(INTERNAL_ASSET_ID, ALICE),
				alice_internal_before - 100 * INTERNAL_UNIT
			);
			assert_eq!(
				PsmDebt::<Test>::get(INTERNAL_ASSET_ID, USDX_ASSET_ID),
				debt_before - 100 * INTERNAL_UNIT
			);
		});
	}
```
