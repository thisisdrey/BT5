### Title
`redeem` skips the live-decimals guard that `mint` enforces, letting a stale decimals snapshot corrupt the PSM's internal/external exchange scale - (File: `substrate/frame/psm/src/lib.rs`)

### Summary
`pallet-psm`'s `mint` extrinsic protects its unit-scaling math by calling `Self::ensure_decimals_match` before converting between external-asset units and internal-asset units, `substrate/frame/psm/src/lib.rs:716-717`. The sibling `redeem` extrinsic performs the exact same scaling conversions (`internal_to_external` / `external_to_internal`) but never calls `ensure_decimals_match`, instead trusting the decimals recorded at registration time (`external.decimals`, `info.internal_decimals`) unconditionally, `substrate/frame/psm/src/lib.rs:825-846`. This is the same class of bug as the reported `GasOracle` issue: a scaling factor derived from an assumed/cached decimals value is used for a monetary conversion without re-validating it against the live, authoritative source, so if the live value diverges the conversion silently produces a wrong price/scale.

### Finding Description
`pallet-psm` stores a decimals snapshot for each internal asset (`PsmInfo::internal_decimals`) and for each approved external asset (`ExternalAssetInfo::decimals`) at registration time. All monetary conversions between external and internal units go through `external_to_internal` / `internal_to_external`, which scale by `10^|ext_decimals - internal_decimals|`, `substrate/frame/psm/src/lib.rs:1580-1624`.

The pallet's own designers recognized that these snapshots can drift from the live decimals reported by `T::Fungibles::decimals()` (an asset's metadata decimals are not immutable, they can be updated later via `pallet_assets::set_metadata`, which is callable by the asset's non-privileged `Owner` — not by PSM governance). To guard against this drift, `ensure_decimals_match` was added and is called in `mint`:

```
substrate/frame/psm/src/lib.rs:716-717
let (ext_decimals, internal_decimals) =
    Self::ensure_decimals_match(&info, &internal_asset, &external_asset, &external)?;
```
`ensure_decimals_match` (`substrate/frame/psm/src/lib.rs:1636-1651`) compares the live `T::Fungibles::decimals()` value for both the internal and external asset against the stored snapshot, and returns `Error::DecimalsMismatch` on divergence, halting that asset until governance intervenes (per the pallet's own documentation in `prdoc/stable2606/pr_11819.prdoc`).

`redeem`, however, reads the snapshot decimals directly without any live check:
```
substrate/frame/psm/src/lib.rs:825-826
let ext_decimals = external.decimals;
let internal_decimals = info.internal_decimals;
```
and proceeds straight to `internal_to_external` / `external_to_internal` (`substrate/frame/psm/src/lib.rs:836,846`) using these potentially stale values, exactly mirroring the `GasOracle.latestAnswer()` pattern of assuming a fixed/cached scale instead of re-deriving it from the authoritative source at the time of use.

### Impact Explanation
If the live decimals of the external asset (or, in principle, the internal asset) diverge from the value snapshotted at registration — which is entirely possible since `pallet_assets::set_metadata` is callable by the asset's ordinary `Owner`, an unprivileged, non-governance actor relative to the PSM — then `redeem`'s scaling factor is wrong while `mint`'s is correctly blocked. Since raw token balances (the actual value moved by `T::Fungibles::transfer`/`burn_from`) are unaffected by a decimals metadata change, an incorrect scaling factor causes `redeem` to compute a materially wrong `external_out` for a given `internal_net` burned. Depending on the direction of the decimals change, an attacker can redeem far more external collateral out of the PSM reserve per unit of internal asset burned than intended, directly draining PSM-held collateral (theft of backing collateral), or conversely could permanently corrupt the exchange ratio to the detriment of legitimate redeemers (fund lock/loss). This is a direct violation of the "conserve value and settle exactly once for the rightful amount" invariant for asset accounting.

### Likelihood Explanation
The path requires no privileged actor: any account that controls (or can influence) the asset metadata of an *already-approved* external asset can trigger the divergence by calling the standard `pallet_assets::set_metadata` extrinsic on their own asset (asset ownership is not the same privilege level as PSM admin/governance), then call the fully public `redeem` extrinsic. `mint` is explicitly hardened against this exact scenario (`ensure_decimals_match` plus `Error::DecimalsMismatch`), which shows the PSM authors treated decimals drift as a real, expected threat — but the mitigation was not applied symmetrically to `redeem`, leaving the sanctioned entry point unprotected.

### Recommendation
Call `Self::ensure_decimals_match(&info, &internal_asset, &external_asset, &external)?` (or an equivalent live-decimals check) at the start of `redeem`, exactly as done in `mint`, before performing `internal_to_external`/`external_to_internal` conversions. This ensures `redeem` halts with `Error::DecimalsMismatch` whenever the live decimals of either asset have diverged from the snapshot, consistent with `mint`'s behavior, and prevents stale-scale arbitrage.

### Proof of Concept
1. PSM admin registers external asset `X` with live decimals `D1` for internal asset `Y` (internal decimals `Di`); `ExternalAssetInfo::decimals = D1` is snapshotted, `PsmDebt` starts accruing as users `mint` normally, building up a reserve of `X` in the PSM account.
2. The owner of asset `X` (an ordinary, non-PSM-governance account with `Owner` rights over that asset in `pallet-assets`) calls `pallet_assets::set_metadata` to change `X`'s live `decimals` to `D2 ≠ D1`. This does not touch any raw balances, only presentation metadata.
3. Any user (attacker) with internal asset `Y` calls `redeem(internal_asset=Y, external_asset=X, internal_amount, max_fee)`.
   - `redeem` reads `ext_decimals = external.decimals` = stale `D1` (not the live `D2`), never calling `ensure_decimals_match`.
   - `internal_to_external(internal_net, D1, Di)` computes `external_out` using the wrong scaling factor relative to what `D2` (the currently intended real-world scale) would require.
4. Compare with attempting the same scenario via `mint`: `mint` would call `ensure_decimals_match`, detect `T::Fungibles::decimals(X) (D2) != external.decimals (D1)`, and revert with `Error::DecimalsMismatch` — proving the guard exists and is effective, but is simply missing from `redeem`.
5. Depending on whether `D2 > D1` or `D2 < D1`, the attacker extracts disproportionately more `X` from the PSM reserve per unit of `Y` burned than the PSM intended, draining backing collateral, or conversely permanently miscalculates the correct redemption amount for legitimate users. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

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

**File:** substrate/frame/psm/src/lib.rs (L1580-1624)
```rust
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

**File:** prdoc/stable2606/pr_11819.prdoc (L1-22)
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
```
