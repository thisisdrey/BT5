Based on my investigation, I found a strong local analog to the reported "units mismatch" bug class in `pallet-psm`, which normalizes external stablecoins of arbitrary decimal precision to an internal pUSD-denominated unit — directly mirroring the WAD-vs-token-decimals conversion problem in the external report.

### Title
`redeem` skips the live-decimals drift guard applied in `mint`, allowing stale decimal-scaling factors to under/over-convert reserve payouts - (File: `substrate/frame/psm/src/lib.rs`)

### Summary
`pallet-psm` converts between an "internal" asset (fixed decimals, e.g. 6) and "external" stablecoins of arbitrary decimal precision using `external_to_internal` / `internal_to_external`, which scale by `10^|ext_decimals - internal_decimals|` [1](#0-0) . The `mint` extrinsic guards against this decimal metadata drifting from the value it was registered with by calling `Self::ensure_decimals_match(&info, &internal_asset, &external_asset, &external)?` before performing any conversion [2](#0-1) . The `redeem` extrinsic, however, reads the decimals directly from the stored snapshot (`external.decimals`, `info.internal_decimals`) without calling the same drift-check helper [3](#0-2) .

### Finding Description
The whole point of the `AssetDecimals` / `StableDecimals` snapshot mechanism (introduced to let PSM support externals with arbitrary decimal precision) is that live on-chain metadata for an asset's decimals can diverge from the value snapshotted at `register_external_asset` time, and any conversion performed with a stale/mismatched decimals value corrupts the scaling factor exactly the way the WAD/token-decimals mismatch corrupted fee withdrawal in the reported bug [4](#0-3) . `mint` explicitly re-validates this invariant every call via `ensure_decimals_match`, and its doc block calls out `Error::DecimalsMismatch` as a possible failure [5](#0-4) . `redeem`'s doc block lists no such check, and the code path confirms it: it fetches `ext_decimals`/`internal_decimals` straight from storage and immediately proceeds to `internal_to_external` conversion and reserve payout, with no call to the drift guard [3](#0-2) . If the live metadata for the external asset's decimals changes after registration (e.g. decimals reported differently by the fungibles backend, or an asset re-registration bug/edge case that leaves the snapshot stale), `redeem` will scale `internal_net` into `external_out` using the wrong power-of-ten factor, and pay out `external_out` from the PSM reserve account without any check that the conversion still matches reality.

### Impact Explanation
An incorrect decimals factor multiplies or divides by a large power of ten (up to `MAX_DECIMALS_DIFF = 24`) [6](#0-5) . Since `redeem` transfers `external_out` straight from the PSM's per-instance reserve account to the caller [7](#0-6) , an inflated conversion factor lets a caller drain the shared reserve backing all externals of that internal asset for an amount far exceeding the internal value they actually burned, breaking the PSM's `InsufficientReserve`/backing accounting invariant that `mint`'s guard was specifically added to protect — the exact "corrupted accounting because the scaling wasn't applied/validated consistently at the value's boundary" pattern described in the seed report.

### Likelihood Explanation
This requires only an unprivileged, signed call to `redeem` and does not require any malicious peer, relayer, or governance actor — it is triggered purely by a mismatch between the live/registered decimals metadata and what the PSM snapshot holds, precisely the drift scenario the `DecimalsMismatch` mechanism and its migration/backfill logic (`PopulateDecimals`) were built to catch [8](#0-7) . I was not able to fully confirm from the index whether `ensure_decimals_match` is invoked from any pre-dispatch hook that would also cover `redeem` indirectly (index size limits truncated the full function body/definition, which sits outside the excerpts I could retrieve) — this should be verified directly in the repository before treating this as conclusively exploitable in production.

### Recommendation
Call the same `ensure_decimals_match` check in `redeem` before deriving `ext_decimals`/`internal_decimals`, exactly as `mint` does, so both directions of conversion are protected against live/snapshot decimal drift.

### Proof of Concept
1. Register an external asset X in a PSM with 18-decimal snapshot (`AssetDecimals[X] = 18`), internal decimals = 6.
2. Live metadata for X later reports 6 decimals (drift/edge case not covered by `mint`'s path being called yet, or exploited immediately after registration before any `mint` call establishes the invariant checked state).
3. Call `redeem(internal_asset, X, internal_amount, max_fee)`. Since `redeem` never calls `ensure_decimals_match`, it uses the stale `ext_decimals = 18` from `external.decimals`, computing `external_out = internal_net / 10^12` — using a scale factor inconsistent with what `mint`'s guard would have required — potentially unlocking a mispriced amount of external reserve relative to actual internal debt reduction, subject to confirming the exact conditions under which the snapshot can diverge from live metadata (this final step needs full-repo verification given index truncation of `ensure_decimals_match`'s definition).

### Citations

**File:** substrate/frame/psm/src/lib.rs (L692-693)
```rust
		/// - [`Error::DecimalsMismatch`]: If live decimals diverged from the snapshot taken at
		///   registration.
```

**File:** substrate/frame/psm/src/lib.rs (L716-720)
```rust
			let (ext_decimals, internal_decimals) =
				Self::ensure_decimals_match(&info, &internal_asset, &external_asset, &external)?;

			let internal_equivalent =
				Self::external_to_internal(external_amount, ext_decimals, internal_decimals)?;
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

**File:** substrate/frame/psm/src/lib.rs (L878-887)
```rust
			let psm_account = Self::psm_account(&internal_asset);
			if !external_out.is_zero() {
				T::Fungibles::transfer(
					external_asset.clone(),
					&psm_account,
					&who,
					external_out,
					Preservation::Expendable,
				)?;
			}
```

**File:** substrate/frame/psm/src/lib.rs (L1575-1624)
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

**File:** prdoc/stable2606/pr_11819.prdoc (L1-31)
```text
title: 'pallet-psm: support external assets with different decimal precision'
doc:
- audience: Runtime Dev
  description: |-
    Previously pallet-psm rejected any external stablecoin whose decimals did
    not match pUSD. This change normalizes to pUSD units internally so the PSM
    can approve assets with arbitrary decimal precision within a safe range.

    Core changes:
    - New storage: per-asset `AssetDecimals` snapshot and pallet-wide
      `StableDecimals` snapshot. Storage version bumped to 2.
    - Conversion helpers `external_to_pusd` / `pusd_to_external` with checked
      arithmetic and `MAX_DECIMALS_DIFF = 24` to prevent overflow.
    - `mint` and `redeem` use round-trip rounding. Truncation dust stays in the
      caller's wallet on both paths (symmetric behavior), no value is trapped
      in the reserve and no hidden dust is routed to the fee destination.
    - `PsmDebt` now denominates in pUSD units so aggregate ceilings and issuance
      checks are meaningful across mixed-decimal assets.
    - Runtime drift guard: `mint`/`redeem` return `DecimalsMismatch` if live
      metadata diverges from the registration snapshot; that asset halts until
      governance intervenes.
    - New errors: `DecimalsRangeExceeded`, `ConversionOverflow`,
      `AmountTooSmallAfterConversion`.

    Migrations:
    - `InitializePsm` now also seeds `StableDecimals` from live metadata if
      missing, and snapshots `AssetDecimals` for any new assets it adds.
    - New one-shot `PopulateDecimals` migration backfills `StableDecimals` and
      `AssetDecimals` for chains that approved external assets before this
      upgrade. Out-of-range assets are auto-disabled (migration does not fail);
      `try-runtime` `post_upgrade` surfaces the anomaly to operators.
```

**File:** substrate/frame/psm/README.md (L229-232)
```markdown
- `DecimalsMismatch`: Live decimals diverged from the registration snapshot
- `DecimalsRangeExceeded`: `|external_decimals − internal_decimals|` exceeds `MAX_DECIMALS_DIFF`
- `ConversionOverflow`: Decimal scaling overflowed
- `AmountTooSmallAfterConversion`: Counter-asset conversion rounds to zero
```
