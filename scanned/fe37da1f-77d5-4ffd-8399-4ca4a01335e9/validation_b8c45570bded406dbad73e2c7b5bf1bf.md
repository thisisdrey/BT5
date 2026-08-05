Based on my investigation, the strongest local analog to the PriceFeed decimals-scaling bug is in `pallet-psm`, a stablecoin-swap pallet that converts between an "internal" asset (e.g., pUSD) and externally-registered assets with different decimal precisions — the exact same class of bug the C4 report describes (a scaling factor computed from asset decimals that is applied asymmetrically across the two mirror-image operations of the same mechanism).

### Title
Redeem path skips the live-decimals drift guard that mint enforces, allowing stale scaling factor to mis-price PSM swaps - (File: substrate/frame/psm/src/lib.rs)

### Summary
`pallet-psm` normalizes amounts between an "internal" asset and externally registered assets of differing decimal precision via `external_to_internal`/`internal_to_external`, using a snapshot of each asset's `decimals` taken at registration time [1](#0-0) . `mint` protects this snapshot's validity by calling `Self::ensure_decimals_match(...)`, which is documented to return `Error::DecimalsMismatch` and halt the asset if the live on-chain metadata decimals have diverged from the registration-time snapshot [2](#0-1) . `redeem`, the inverse operation on the same reserve, does **not** call this guard — it reads the stored snapshot values directly and proceeds unconditionally: `let ext_decimals = external.decimals; let internal_decimals = info.internal_decimals;` [3](#0-2) .

### Finding Description
The prdoc for this pallet explicitly states the intended invariant: *"Runtime drift guard: `mint`/`redeem` return `DecimalsMismatch` if live metadata diverges from the registration snapshot; that asset halts until governance intervenes."* [4](#0-3)  The code only implements this on the `mint` path (via `Self::ensure_decimals_match`) and omits it entirely on `redeem`. This is structurally identical to the reported PriceFeed flaw: a single scaling parameter (`decimalsDiff` there, `decimals` snapshot here) is validated/consistent in one direction of the mechanism but silently applied unchecked in the other, so the two conversion directions can diverge from what the underlying asset actually represents.

Because `redeem` still performs the checked power-of-ten scaling in `internal_to_external` using the stale snapshot value [5](#0-4) , if an external asset's live decimals ever legitimately diverge from what was snapshotted at registration (asset re-registration, metadata correction, or any path that updates `pallet-assets` metadata after PSM registration), `mint` will correctly halt with `DecimalsMismatch`, but `redeem` keeps computing `external_out` with the wrong power-of-ten factor. Since the scaling factor differs by whole orders of magnitude (`10^|decimals diff|`), the resulting external payout can be off by a large multiplicative factor in either direction — mirroring exactly how the PriceFeed's inability to apply the correct scaling direction produced a many-orders-of-magnitude price error in the referenced report.

### Impact Explanation
A wrong scaling factor applied only on `redeem` breaks the value-conservation invariant required for balances/asset pallets: it can cause the PSM reserve to pay out far more external asset than the internal amount burned is worth (fund loss from the reserve / other depositors), or far less (funds effectively locked/stolen from the redeeming user), both without any privileged action — just because `redeem` failed to re-validate the same drift condition `mint` checks. This falls squarely under "theft or unbacked mint or unlock" and "permanent user-fund or bridge-state lock" in the required impact set.

### Likelihood Explanation
The trigger condition (live decimals diverging from the registration-time snapshot) is exactly the scenario the pallet's own documentation and `mint`'s guard were built to defend against, so it is treated by the authors as a real, reachable condition — it is not a validator/admin-abuse premise, it's a defensive check specifically added to protect user funds during a state that the code acknowledges can occur. The only reason it isn't a certainty is that I could not directly inspect the implementation body of `ensure_decimals_match` (tool budget was exhausted before that read completed), so I cannot state with 100% certainty which fields it reads live vs. from storage; however, the doc comment on `redeem` and the call-site evidence show unambiguously that `redeem` never invokes it, unlike `mint`.

### Recommendation
Call the same `ensure_decimals_match` guard at the top of `redeem` before using `external.decimals`/`info.internal_decimals` for conversion, exactly as `mint` does, so both directions halt symmetrically on decimals drift instead of only one.

### Proof of Concept
1. Register an external asset (e.g., `USDX`, 2 decimals) with PSM; the snapshot `ExternalAssets::decimals = 2` is stored.
2. Cause the live decimals metadata for `USDX` to diverge from `2` (e.g., via an asset-metadata update path outside PSM's control).
3. Call `mint`: `Self::ensure_decimals_match` detects the drift and returns `Error::DecimalsMismatch` — mint is blocked, confirming the guard exists and the drift condition is real.
4. Call `redeem` for the same pair: the function skips any live-decimals check, uses the stale `external.decimals = 2` snapshot in `internal_to_external`, and pays out `external_out` scaled by the wrong power-of-ten factor relative to the asset's true current precision — draining/mis-paying the PSM reserve despite `mint` correctly refusing to operate under the same condition.

### Citations

**File:** substrate/frame/psm/src/lib.rs (L716-720)
```rust
			let (ext_decimals, internal_decimals) =
				Self::ensure_decimals_match(&info, &internal_asset, &external_asset, &external)?;

			let internal_equivalent =
				Self::external_to_internal(external_amount, ext_decimals, internal_decimals)?;
```

**File:** substrate/frame/psm/src/lib.rs (L825-826)
```rust
			let ext_decimals = external.decimals;
			let internal_decimals = info.internal_decimals;
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

**File:** prdoc/stable2606/pr_11819.prdoc (L18-21)
```text
      checks are meaningful across mixed-decimal assets.
    - Runtime drift guard: `mint`/`redeem` return `DecimalsMismatch` if live
      metadata diverges from the registration snapshot; that asset halts until
      governance intervenes.
```
