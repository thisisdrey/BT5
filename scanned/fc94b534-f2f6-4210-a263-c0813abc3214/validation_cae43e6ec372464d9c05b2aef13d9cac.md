### Title
`pallet-psm::redeem()` skips the live-decimals validation enforced by `mint()`, letting reserve funds be drained via stale decimal conversion - (File: `substrate/frame/psm/src/lib.rs`)

### Summary
`pallet-psm` normalizes cross-decimal conversions between an "internal" stablecoin and "external" assets using snapshotted decimals stored at registration time. `mint()` re-validates that the live decimals of both assets still match this snapshot via `Self::ensure_decimals_match(...)` before converting amounts [1](#0-0) , and halts with `DecimalsMismatch` if they diverge [2](#0-1) . `redeem()`, however, reads the snapshotted decimals directly and performs the identical conversion arithmetic without calling this guard at all [3](#0-2) .

### Finding Description
The pallet's conversion helpers `external_to_internal`/`internal_to_external` scale amounts by `10^(decimals diff)` [4](#0-3) . Both `mint()` and `redeem()` rely on decimals values (`ext_decimals`, `internal_decimals`) that were snapshotted when the PSM/external pair was registered (`PsmInfo::internal_decimals`, `ExternalAssetInfo::decimals`), not the live decimals reported by the fungibles backend.

`mint()` explicitly protects against decimal drift: it calls `ensure_decimals_match`, which compares the snapshot against `T::Fungibles::decimals(...)` for both the internal and external asset and returns `Error::DecimalsMismatch` on divergence, halting minting until governance intervenes (as also documented in the corresponding prdoc: "Runtime drift guard: `mint`/`redeem` return `DecimalsMismatch`...") [5](#0-4) .

`redeem()`'s doc comment claims this guard is intentionally bypassed "allowing existing positions to unwind even if live metadata later changes" [6](#0-5) , and the implementation confirms it: it pulls `ext_decimals`/`internal_decimals` straight from the stored snapshot and feeds them into `internal_to_external`/`external_to_internal` without any equality check against the current live decimals [7](#0-6) .

The problem is that the fungibles backend (`pallet-assets`) allows an asset's decimals metadata to be updated post-creation (via `set_metadata`), independent of PSM governance. If the live decimals of the external (or internal) asset change after PSM registration — for any reason, not necessarily malicious admin action on the PSM itself — the PSM's reserve now holds/accounts tokens whose real unit scale differs from the snapshot, but `redeem()` continues to use the stale scale factor to compute `external_out` from the actual on-chain reserve. This is the exact same broken invariant as H-11: an amount is passed through a cross-domain conversion using an assumed decimals value that no longer matches reality, producing a wildly wrong output amount (`10^n` too large or too small) while the transfer executes against the *real* token unit.

Unlike H-11's cross-chain message (where the mismatch is inherent to token behavior), this local analog is triggered purely by decimals drift that `mint()` already anticipates and defends against — `redeem()`'s omission is a straightforward asymmetric-guard bug, not a "malicious asset owner" precondition; it is a public, unprivileged dispatchable (`ensure_signed` only) that any PSM user can call at any time decimals drift occurs.

### Impact Explanation
If decimals drift occurs after registration, `redeem()` computes `external_out` using the wrong scale factor while `T::Fungibles::transfer` moves tokens in the asset's *actual* current unit. If live decimals increased relative to the snapshot, the reserve check (`reserve < external_out`) may still pass numerically, but the pallet will pay out an amount that is off by orders of magnitude, draining the PSM's reserve for a comparatively tiny burn of internal stablecoin — a direct, unrecoverable fund loss from the protocol's reserve to an ordinary user. Conversely, if decimals decreased, redeemers would receive negligible external amounts, permanently locking value inside the PSM. Both directions mirror H-11's "massive overborrowing or tiny underborrowing" impact, but manifest here as reserve drain or fund lock, without requiring any privileged or malicious PSM-side actor.

### Likelihood Explanation
Triggering this requires only that an approved external (or internal) asset's live decimals diverge from what was snapshotted at PSM registration time — something `mint()` is explicitly built to detect and block, implying the pallet authors consider decimals drift a realistic operational event (asset metadata correction, asset re-creation, etc.). Once drift occurs, exploitation via `redeem()` requires nothing beyond a signed call with an internal balance to burn; no governance, validator, or relayer collusion is needed.

### Recommendation
Apply the same `ensure_decimals_match` (or an equivalent live-decimals check) in `redeem()` as is already enforced in `mint()`, or, if intentionally allowing snapshot-based redemption for orderly unwind, cap/limit the scale-factor divergence risk (e.g., pause redemption too on drift, or track decimals drift explicitly and adjust conversions safely) rather than silently trusting stale values against a live reserve balance.

### Proof of Concept
1. Register `internal_asset` (18 decimals) and `external_asset` (6 decimals) on a PSM; `ExternalAssetInfo::decimals = 6` is snapshotted.
2. User mints normally, depositing external asset into the PSM reserve (6-decimal units).
3. External asset's live decimals are updated (via `pallet-assets::set_metadata`) from 6 to 18 — this makes `mint()` immediately revert with `DecimalsMismatch` per `ensure_decimals_match`, but does **not** affect `redeem()`.
4. Attacker calls `redeem()` with a small `internal_amount`. `redeem()` uses the *snapshotted* `ext_decimals = 6` to compute `external_out = internal_to_external(internal_net, 6, 18)`, multiplying up by `10^12` relative to what the true (now 18-decimal) reserve balance actually represents.
5. `T::Fungibles::transfer` pays out `external_out` in the asset's real (now 18-decimal) unit, draining far more value from the reserve than the burned internal amount is worth — reproducing the H-11 impact locally.

### Citations

**File:** substrate/frame/psm/src/lib.rs (L716-717)
```rust
			let (ext_decimals, internal_decimals) =
				Self::ensure_decimals_match(&info, &internal_asset, &external_asset, &external)?;
```

**File:** substrate/frame/psm/src/lib.rs (L781-783)
```rust
		/// undercharges. Redemptions use the decimals snapshotted when the PSM/external pair
		/// was registered, allowing existing positions to unwind even if live metadata later
		/// changes.
```

**File:** substrate/frame/psm/src/lib.rs (L825-846)
```rust
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

**File:** prdoc/stable2606/pr_11819.prdoc (L18-21)
```text
      checks are meaningful across mixed-decimal assets.
    - Runtime drift guard: `mint`/`redeem` return `DecimalsMismatch` if live
      metadata diverges from the registration snapshot; that asset halts until
      governance intervenes.
```
