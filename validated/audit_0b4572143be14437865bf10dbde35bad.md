### Title
`pallet-psm::redeem()` skips the live-decimals drift guard that `mint()` enforces, allowing mis-scaled internal/external conversion - (File: `substrate/frame/psm/src/lib.rs`)

### Summary
This is a direct local analog of the reported bug class: a contract/pallet that aggregates value across assets with **different decimals** using a shared conversion path, but only applies its decimals-integrity check on one code path. `pallet-psm` normalizes external stablecoins with arbitrary decimals to a common internal-asset unit via `external_to_internal`/`internal_to_external`, guarded by a `DecimalsMismatch` check (`ensure_decimals_match`) that is supposed to halt swaps if live asset metadata has drifted from the decimals snapshot taken at registration. That guard is called in `mint()` but is **not** called in `redeem()`.

### Finding Description
`mint()` calls `Self::ensure_decimals_match(&info, &internal_asset, &external_asset, &external)?` before doing any decimal conversion: [1](#0-0) 

`redeem()`, however, pulls the decimals straight from the stored snapshot without re-validating them against live metadata: [2](#0-1) 

The conversion helpers (`external_to_internal` / `internal_to_external`) scale amounts by `10^|ext_decimals - internal_decimals|`, and `PsmDebt` is tracked per `(internal_asset, external_asset)` in internal-asset units, exactly like the reported contract's per-token claimed-amount/threshold mappings: [3](#0-2) [4](#0-3) 

The pallet's own change-doc explicitly states the intended invariant applies to **both** entry points: "Runtime drift guard: `mint`/`redeem` return `DecimalsMismatch` if live metadata diverges from the registration snapshot; that asset halts until governance intervenes." [5](#0-4) 

Because `redeem()` never calls `ensure_decimals_match`, if the live decimals reported by `T::Fungibles::decimals()` for `external_asset` (or the internal asset) diverge from the `PsmInfo::internal_decimals` / `ExternalAssetInfo::decimals` snapshot taken at approval time, `redeem()` will silently keep using the stale snapshot to compute `internal_to_external` / `external_to_internal`. This produces a **decimals-mismatched conversion factor** exactly analogous to the report's exploit: the same nominal `internal_amount` burn now redeems a disproportionately large or small amount of `external_asset`, because the scaling factor no longer reflects the token's real decimal precision.

### Impact Explanation
This breaks the PSM's core "1:1 backed" invariant (`PsmDebt` is supposed to represent exactly the internal-asset amount backed by reserve). A mismatched decimals factor on the redeem path lets a user extract more `external_asset` from the reserve than the burned `internal_asset` amount actually backs, or drain the reserve while under-decrementing `PsmDebt`, i.e., unbacked/duplicate settlement of value — falling squarely under "theft or unbacked mint/unlock" and "public wrappers/settlement not conserving value" in the impact gate. The `InsufficientReserve` and reserve-balance defensive check (`reserve < external_out`) only catch gross reserve exhaustion; they do not catch a subtly wrong per-unit conversion factor that still nets in the attacker's favor before the reserve is fully drained.

### Likelihood Explanation
Exploitability is fully dependent on an unprivileged/normal signed caller of `redeem()` (a permissionless, public extrinsic) once decimals drift exists between the `mint`-time snapshot and the live `Fungibles::decimals()` value for that asset — no relayer, validator, or governance actor is required to trigger the missing check itself. The remaining open question, which I could not fully verify from the indexed code, is the exact operational precondition that makes live decimals diverge from the snapshot (e.g., asset id reuse after `remove_external_asset`/re-registration with a differently-decimaled asset, or a fungibles backend where decimals are mutable/re-derived). This mechanism deserves verification with a Devin session that can inspect the full `add_external_asset`/`remove_external_asset` lifecycle and the specific `Fungibles` backend used in production runtimes.

### Recommendation
Call `Self::ensure_decimals_match(&info, &internal_asset, &external_asset, &external)?` in `redeem()` exactly as `mint()` does, before performing `internal_to_external`/`external_to_internal` conversions, so both entry points halt on `Error::DecimalsMismatch` consistently.

### Proof of Concept
1. Register `external_asset` on a PSM instance; `ExternalAssetInfo::decimals` snapshot is taken (e.g., 6).
2. Cause the live decimals reported by `T::Fungibles::decimals(external_asset)` to change (e.g., via asset id reuse/re-registration through whatever governance/asset-pallet path makes decimals mutable for that asset id) to a different value (e.g., 18), without going through `mint()` (which would revert with `DecimalsMismatch`).
3. Call `Psm::redeem(origin, internal_asset, external_asset, internal_amount, max_fee)`. Since `redeem()` never re-checks decimals, it computes `external_out` using the stale snapshot decimals (6) against a fungibles balance now denominated with 18 decimals, producing an output magnitude off by `10^12` from the correct value, draining the reserve or bypassing the intended 1:1 backing relative to `PsmDebt`.

### Citations

**File:** substrate/frame/psm/src/lib.rs (L712-720)
```rust
			let external = ExternalAssets::<T>::get(&internal_asset, &external_asset)
				.ok_or(Error::<T>::UnsupportedAsset)?;
			ensure!(external.status.allows_minting(), Error::<T>::MintingStopped);

			let (ext_decimals, internal_decimals) =
				Self::ensure_decimals_match(&info, &internal_asset, &external_asset, &external)?;

			let internal_equivalent =
				Self::external_to_internal(external_amount, ext_decimals, internal_decimals)?;
```

**File:** substrate/frame/psm/src/lib.rs (L819-836)
```rust
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
```

**File:** substrate/frame/psm/src/lib.rs (L1575-1599)
```rust
		/// Convert an amount denominated in external-asset units into internal units.
		///
		/// Scales by `10^(ext_decimals - internal_decimals)` — multiplies up when internal has more
		/// decimals, floor-divides when it has fewer. Returns [`Error::ConversionOverflow`] if
		/// the scaling factor or the product does not fit in the balance type.
		pub(crate) fn external_to_internal(
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
					amount.checked_mul(&factor).ok_or(Error::<T>::ConversionOverflow)
				},
				Greater => {
					let diff = (ext_decimals - internal_decimals) as u32;
					let factor = Self::pow10(diff)?;
					Ok(amount.checked_div(&factor).unwrap_or_else(BalanceOf::<T>::zero))
				},
			}
		}
```

**File:** substrate/frame/psm/src/tests.rs (L3172-3201)
```rust
	#[test]
	fn aggregate_debt_accrues_in_internal_units_across_mixed_decimal_assets() {
		new_test_ext().execute_with(|| {
			register_external_asset_with_weight(USDX_ASSET_ID, Permill::from_percent(50));
			register_external_asset_with_weight(DAI_MOCK_ASSET_ID, Permill::from_percent(50));
			set_zero_fees(USDX_ASSET_ID);
			set_zero_fees(DAI_MOCK_ASSET_ID);

			// Mint 500 internal-equivalent via USDX, 1500 internal-equivalent via DAI.
			assert_ok!(Psm::mint(
				RuntimeOrigin::signed(ALICE),
				INTERNAL_ASSET_ID,
				USDX_ASSET_ID,
				500 * USDX_UNIT,
				Permill::zero()
			));
			assert_ok!(Psm::mint(
				RuntimeOrigin::signed(ALICE),
				INTERNAL_ASSET_ID,
				DAI_MOCK_ASSET_ID,
				1500 * DAI_UNIT,
				Permill::zero()
			));

			assert_eq!(PsmDebt::<Test>::get(INTERNAL_ASSET_ID, USDX_ASSET_ID), 500 * INTERNAL_UNIT);
			assert_eq!(
				PsmDebt::<Test>::get(INTERNAL_ASSET_ID, DAI_MOCK_ASSET_ID),
				1500 * INTERNAL_UNIT
			);
			assert_eq!(Psm::total_psm_debt(&INTERNAL_ASSET_ID), 2000 * INTERNAL_UNIT);
```

**File:** prdoc/stable2606/pr_11819.prdoc (L18-21)
```text
      checks are meaningful across mixed-decimal assets.
    - Runtime drift guard: `mint`/`redeem` return `DecimalsMismatch` if live
      metadata diverges from the registration snapshot; that asset halts until
      governance intervenes.
```
