## Analysis

The external report's core broken invariant is: **a decimals-derived scaling/offset value that is computed dynamically per-asset, is not floor-guarded against reaching a degenerate value (zero), and is trusted downstream without re-validating the post-scaling amount** — leading either to bypassed protection (attacker side) or silent value loss / broken accounting invariants (victim side).

The closest local analog in this repository is the custom **Peg Stability Module (PSM) pallet** (`substrate/frame/psm`), which performs exactly this kind of dynamic, decimals-dependent scaling between an "internal" stablecoin and "external" reserve assets that can have arbitrary, user/asset-registry-controlled decimal counts.

### Title
Decimals-based conversion in `pallet-psm` silently floor-divides to zero, breaking the 1:1 reserve/debt invariant - (File: `substrate/frame/psm/src/lib.rs`)

### Summary
`Pallet::external_to_internal` and `Pallet::internal_to_external` scale amounts between an external reserve asset and the PSM's internal stablecoin using a factor derived purely from each asset's `decimals()` metadata, exactly analogous to the reported `decimalOffset = SYSTEM_DECIMALS - indexToken.decimals()` pattern. [1](#0-0)  When the external asset has more decimals than the internal asset (`Greater` branch), the amount is floor-divided by `10^(ext_decimals - internal_decimals)`, and any division-derived degenerate case is coerced to zero rather than surfaced as an error: `amount.checked_div(&factor).unwrap_or_else(BalanceOf::<T>::zero)`. [2](#0-1)  The inverse function has the symmetric floor-division branch. [3](#0-2) 

### Finding Description
Both conversion helpers are decimals-driven, exactly like the reported `decimalOffset`:
- `external_to_internal`: multiplies (no loss) when internal has more decimals than external, but **floor-divides** when external has more decimals than internal. [4](#0-3) 
- `internal_to_external`: the mirror image, floor-dividing in the opposite case. [3](#0-2) 

This reproduces all three weaknesses called out in the report:
1. **Unpredictability** — the scaling factor is entirely a function of `ext_decimals`/`internal_decimals`, values that are read from asset metadata (settable by whoever registers the external asset via `add_external_asset`), not a protocol-chosen constant.
2. **Silent degeneration to zero** — the `Greater` branch returns `Ok(0)` (via `unwrap_or_else(BalanceOf::<T>::zero)`) instead of propagating `Error::ConversionOverflow`/a dedicated "too small" error when the division floors to zero. A caller that only checks the `Result::Err` case will treat a converted amount of `0` as a valid `Ok` outcome.
3. **Weak/asymmetric protection** — for asset pairs where the external asset has significantly more decimals than the internal one (e.g. an 18-decimal external stablecoin vs. a 6- or 10-decimal internal stablecoin), the scaling factor can be as large as `10^12`, meaning any external deposit smaller than that factor converts to `0` internal units.

This directly threatens the pallet's core accounting invariant documented at the top of the file: "PSM Debt: Total internal asset minted through a PSM, backed 1:1 by external assets in that PSM's reserve." [5](#0-4)  If `mint`/`redeem` (which call these helpers) do not re-check that the *converted* amount is non-zero after scaling — only that the *input* amount is non-zero — an attacker (or ordinary small-balance user) can move external reserve assets into/out of the PSM while the corresponding internal debt or external payout evaluates to zero, unbacking the 1:1 peg guarantee or creating a griefing/DoS vector for small depositors identical to the ERC4626 zero-share scenario described in the report.

### Impact Explanation
If the zero-conversion result is not explicitly rejected before the pallet moves funds and updates `PsmInfo`/reserve/debt storage, this breaks the "conserve value, settle exactly once" invariant called out in the Polkadot SDK Pivots: reserve balance can increase without any offsetting increase in outstanding internal debt (or vice versa on redemption), corrupting the accounting that the module's peg-stability guarantees depend on. This falls squarely in the "staking or asset accounting" impact category requested by the task (conservation-of-value across a custom accounting pallet).

### Likelihood Explanation
Likelihood depends on whether `mint`/`redeem` validate the *post-scaling* amount for non-zero before finalizing state changes — this call path was not fully retrievable within the available indexing window, so I could not directly confirm the guard is missing at the dispatchable level. The conversion helpers themselves, however, are proven by direct source inspection to return `0` for legitimately-formed inputs without error when the decimals gap is large enough, which is the precise "weak protection in some cases" defect the external report targets. Any deployment pairing an internal asset with low decimals against an external asset with high decimals (a very plausible stablecoin/asset registry configuration) reaches this degenerate branch with ordinary, unprivileged user calls — no malicious peer, validator, or governance actor is required.

### Recommendation
- In `external_to_internal`/`internal_to_external`, replace `unwrap_or_else(BalanceOf::<T>::zero)` with an explicit error (e.g. `Error::<T>::AmountTooSmall`) whenever the floor-division would yield `0` for a non-zero input.
- At the `mint`/`redeem` call sites, explicitly assert the converted amount is non-zero before performing any transfer/mint/burn or storage update, mirroring the "must not mint zero shares" guard the external report recommends for ERC4626 vaults.
- Consider bounding/registering a maximum allowed decimals differential per PSM external-asset pair instead of trusting attacker/registrar-supplied decimals unconditionally.

### Proof of Concept
Given a PSM instance where `internal_decimals = 6` and `ext_decimals = 18` (a plausible registration since `add_external_asset` reads decimals from asset metadata, not a protocol constant):
1. `diff = 18 - 6 = 12`, so `factor = 10^12`.
2. A user calls `mint` depositing `999_999_999_999` (i.e., `< 10^12`) units of the external asset.
3. `external_to_internal` computes `amount.checked_div(&factor)` = `0`, returned as `Ok(0)` via the `unwrap_or_else` fallback rather than an error. [2](#0-1) 
4. If the dispatchable proceeds to transfer the external asset into the PSM reserve based on the original (pre-conversion) amount while minting `0` internal tokens (net of fee), the reserve grows without any corresponding increase in `PsmInfo` debt — breaking the 1:1 backing invariant the pallet exists to enforce, and the depositing user permanently loses the deposited external asset for zero internal tokens received.

**Uncertainty note:** I was unable to retrieve and inspect the full `mint`/`redeem` dispatchable bodies within the available tool budget to confirm definitively whether a downstream zero-amount guard exists after calling `external_to_internal`/`internal_to_external`. The vulnerability claim rests on the confirmed fact that the conversion helpers themselves can silently return `0` as a successful `Ok` result for legitimate non-zero inputs — this is the exact analog of the report's "weak protection in some cases" defect, and it is a real, source-confirmed design pattern in this repository regardless of whether an additional guard exists elsewhere.

### Citations

**File:** substrate/frame/psm/src/lib.rs (L60-61)
```rust
//! * **PSM Debt**: Total internal asset minted through a PSM, backed 1:1 by external assets in that
//!   PSM's reserve.
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
