## Finding: `pallet-psm::redeem` skips the decimals-drift guard that `mint` enforces, allowing stale-decimal scaling to mis-price redemptions

### Title
Missing `ensure_decimals_match` check in `redeem` lets stale decimal snapshots corrupt external-asset payout scaling - (File: `substrate/frame/psm/src/lib.rs`)

### Summary
The external report's core invariant break is: a conversion path applies a decimal-scaling factor derived from assumed/stale precision instead of validating it against the live asset's actual precision, producing a mis-scaled value. `pallet-psm` implements exactly this decimal-scaling logic for external/internal asset conversion, and enforces a live-vs-snapshot decimals check (`ensure_decimals_match`, surfaced via `Error::DecimalsMismatch`) on the `mint` path but not on the `redeem` path.

### Finding Description
`pallet-psm` converts between an "internal" asset (fixed decimals) and arbitrary-decimal "external" assets using a per-asset decimals snapshot taken at registration time (`ExternalAssets::decimals`) combined with `PsmInfo::internal_decimals`, via `external_to_internal` / `internal_to_external`: [1](#0-0) 

The `mint` extrinsic explicitly guards against decimals drift by calling `ensure_decimals_match`, which (per its use and the pallet's documented "Runtime drift guard") is meant to fail with `Error::DecimalsMismatch` if the live metadata for the external/internal asset diverges from the registration-time snapshot: [2](#0-1) 

The `redeem` extrinsic, however, reads the decimals directly from the stored snapshot (`external.decimals`, `info.internal_decimals`) without calling `ensure_decimals_match` at all: [3](#0-2) 

This is the direct analog of the BloomPool bug: a scaling factor (here, `10^(ext_decimals - internal_decimals)` used inside `internal_to_external`/`external_to_internal`) is applied based on a value (`external.decimals`) that is not re-validated against ground truth at the point of use, only on one of the two symmetric code paths. If the live decimals of the external asset (queried via the fungibles/assets backend, e.g. by an asset owner using `pallet-assets`' metadata-setting extrinsics for assets they control, or by any mechanism that can alter an asset's reported `decimals` after PSM registration) diverge from the snapshot stored in `ExternalAssets`, `mint` will correctly reject with `DecimalsMismatch`, but `redeem` will silently proceed using the stale, wrong decimals value to compute the scaling factor in `internal_to_external`, producing an incorrect `external_out` amount.

### Impact Explanation
Because `internal_to_external`/`external_to_internal` scale by powers of 10 derived from the (potentially stale) decimals difference, a mismatch of even one decimal place multiplies or divides the resulting external amount by 10x (or more, for larger drifts). Since `redeem` transfers `external_out` from the PSM reserve account to the caller (`T::Fungibles::transfer(external_asset, &psm_account, &who, external_out, ...)`) while only burning `effective_internal_net` computed from the same stale-scaled round trip, this can let a caller extract far more external-asset value from the shared PSM reserve than the internal asset they burn is worth — draining the reserve at the expense of other depositors — or conversely could pay out far less than owed, locking value. This directly matches the "theft or unbacked mint or unlock" and "permanent user-fund lock" impact categories, is reachable from a completely public, unprivileged `redeem` call, and requires no malicious validator/relayer/admin.

### Likelihood Explanation
The likelihood is contingent on whether the live decimals of an already-approved external asset can change after PSM registration (e.g., an asset with `Owner`-mutable metadata, or a metadata migration on `pallet-assets`). The pallet's own design (`AssetDecimals`/`StableDecimals` snapshot plus a documented "Runtime drift guard: mint/redeem return DecimalsMismatch if live metadata diverges") shows the maintainers explicitly anticipated this exact drift scenario and intended the guard to apply to *both* mint and redeem — but the code only wires it into `mint`. This is a straightforward asymmetry bug rather than requiring an exotic pre-condition beyond "external asset decimals metadata changes post-registration," which is plausible for any asset not fully immutable/locked.

### Recommendation
Add the same `ensure_decimals_match` (or equivalent live-metadata verification) call at the start of `redeem`, mirroring `mint`, before using `external.decimals`/`info.internal_decimals` to compute `internal_to_external`/`external_to_internal`. Ideally, factor the check into a single helper used by both extrinsics so future changes can't reintroduce the asymmetry.

### Proof of Concept
1. Governance approves external asset `X` with `ext_decimals = 6` via `add_external_asset`/`register_external_asset_with_weight`, snapshotted into `ExternalAssets::decimals`.
2. Owner of asset `X` (or any process capable of mutating asset metadata) later changes `X`'s live decimals to `18`.
3. Call `mint(internal_asset, X, amount, max_fee)` → fails with `Error::DecimalsMismatch` because `ensure_decimals_match` compares live vs. snapshot decimals.
4. Call `redeem(internal_asset, X, internal_amount, max_fee)` → succeeds, because `redeem` never calls `ensure_decimals_match` and instead scales using the stale `ext_decimals = 6` from `ExternalAssets`, computing `external_out` at the wrong (6-decimal) scale against `X`'s real 18-decimal balance representation — resulting in a payout that is off by `10^12`, draining the PSM's external-asset reserve relative to the internal amount actually burned.

### Citations

**File:** substrate/frame/psm/src/lib.rs (L716-722)
```rust
			let (ext_decimals, internal_decimals) =
				Self::ensure_decimals_match(&info, &internal_asset, &external_asset, &external)?;

			let internal_equivalent =
				Self::external_to_internal(external_amount, ext_decimals, internal_decimals)?;
			ensure!(!internal_equivalent.is_zero(), Error::<T>::AmountTooSmallAfterConversion);
			ensure!(internal_equivalent >= info.min_swap_amount, Error::<T>::BelowMinimumSwap);
```

**File:** substrate/frame/psm/src/lib.rs (L821-836)
```rust
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
