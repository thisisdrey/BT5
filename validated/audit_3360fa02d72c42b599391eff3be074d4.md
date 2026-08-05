Audit Report

## Title
Silent zero-value conversion in PSM decimal scaling causes fund loss for small deposits/redemptions - (File: substrate/frame/psm/src/lib.rs)

## Summary
The `external_to_internal` and `internal_to_external` helper functions in `pallet-psm` use `checked_div(&factor).unwrap_or_else(BalanceOf::<T>::zero)` on the down-scaling (division) branch, while the up-scaling (multiplication) branch correctly returns `Error::<T>::ConversionOverflow` on failure. This asymmetry means any amount smaller than the decimal-scaling factor (`10^diff`) silently converts to `Ok(0)` instead of erroring, which can result in a user's deposited asset being retained by the PSM reserve while they receive zero of the counter-asset (or vice versa on redemption).

## Finding Description
`pallet-psm` is documented to enable "1:1 swaps between an internal stablecoin and one or more approved external stablecoins." [1](#0-0) 

The conversion helpers are: [2](#0-1) [3](#0-2) 

In both functions, the multiplication (up-scaling) branch propagates failure via `Error::<T>::ConversionOverflow`, but the division (down-scaling) branch collapses any `None` case (which mathematically cannot occur here since the divisor `10^diff >= 1`) and, more importantly, collapses any legitimate `Some(0)` result — i.e., any input amount smaller than `10^diff` — into a plain `Ok(0)` via `unwrap_or_else(BalanceOf::<T>::zero)`. This treats a fund-losing edge case (input too small to survive scaling) identically to a successful zero-amount conversion, without giving calling dispatchables a distinguishable error to reject on.

## Impact Explanation
If the dispatchable(s) that call these helpers do not independently check for a zero conversion result before executing the transfer/mint/burn against the non-zero input leg, a deposit or redemption of an amount smaller than the decimal gap factor between the two registered assets would allow the pallet to retain/burn a user's real-valued input while crediting them nothing in return — a direct fund-conservation violation matching the "Balances, assets, ... must conserve value and settle exactly once to the rightful beneficiary and amount" pivot.

## Likelihood Explanation
I was able to confirm the exact conversion-helper code exists and behaves as described in the claim, matching it verbatim. However, I was unable to locate and inspect the specific public dispatchable(s) (e.g., swap/deposit/redeem extrinsics) that invoke `external_to_internal`/`internal_to_external` within the available tool budget, so I cannot confirm or rule out whether a downstream zero-check guard already prevents the described fund-loss path. This is a material gap: if such a guard exists (e.g., an `ensure!(!amount.is_zero())` check on the converted output before transferring), the vulnerability as described would not be exploitable end-to-end, even though the helper-level asymmetry itself is real. Given this uncertainty about the full call path, I cannot certify the claim's asserted reachability of "user calls swap extrinsic → funds are lost" as confirmed, only the underlying helper-function defect is confirmed as written.

## Recommendation
Change `external_to_internal`/`internal_to_external` to return an explicit error (e.g., `Error::<T>::AmountTooSmall`) when the division would floor to `0` for a nonzero input amount, mirroring the pattern used in `pallet-asset-conversion`'s hardened `quote_price_exact_tokens_for_tokens`/`quote_price_tokens_for_exact_tokens`. [4](#0-3) 
Additionally, callers of these helpers should be audited to confirm whether a zero-result guard already exists before transfer/mint/burn execution.

## Proof of Concept
1. Configure two PSM-approved assets with a decimal gap (e.g., `external_decimals = 18`, `internal_decimals = 6`, giving `diff = 12`, `factor = 10^12`).
2. Call `external_to_internal(1, 18, 6)`; this hits the `Greater` branch and computes `1u128.checked_div(&10^12) == Some(0)`, returning `Ok(0)` instead of an error.
3. Verification of whether this zero result is checked and rejected by the actual public swap/deposit dispatchable in `substrate/frame/psm/src/lib.rs` remains unconfirmed due to inability to locate and review those call sites within the available tool budget — this should be verified via a full session with unrestricted file access before treating the fund-loss impact as fully proven end-to-end.

### Citations

**File:** substrate/frame/psm/src/lib.rs (L18-21)
```rust
//! # Peg Stability Module (PSM) Pallet
//!
//! Instantiable Peg Stability Modules (PSMs). Each PSM enables 1:1 swaps between an internal
//! stablecoin and one or more approved external stablecoins, typically to maintain a peg.
```

**File:** substrate/frame/psm/src/lib.rs (L1580-1599)
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

**File:** prdoc/stable2606/pr_11795.prdoc (L1-8)
```text
title: "Harden asset-conversion quote functions against zero amounts"
doc:
- audience: Runtime Dev
  description: |-
    Hardens `quote_price_exact_tokens_for_tokens` and `quote_price_tokens_for_exact_tokens` in
    `pallet-asset-conversion` to return `None` for zero input amounts and when integer rounding
    produces a zero output. Previously, zero inputs could propagate through the AMM math and
    zero outputs from small-input rounding were returned as `Some(0)`.
```
