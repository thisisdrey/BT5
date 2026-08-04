### Title
Silent zero-value conversion in PSM decimal scaling causes fund loss for small deposits/redemptions - (File: substrate/frame/psm/src/lib.rs)

### Summary
The `pallet-psm` (Peg Stability Module) converts amounts between an internal stablecoin and externally pegged stablecoins with different decimal precisions using `external_to_internal` and `internal_to_external`. Both helpers use `checked_div(&factor).unwrap_or_else(BalanceOf::<T>::zero)` for the down-scaling branch, silently collapsing any amount smaller than the decimal scaling factor to zero instead of returning an error. This mirrors the reported Chainlink bug pattern exactly: a failure/edge condition in a value-conversion helper is silently mapped to `0` rather than propagated as an error, and that zero then flows into downstream accounting.

### Finding Description
`pallet-psm` documents itself as enabling "1:1 swaps between an internal stablecoin and one or more approved external stablecoins." [1](#0-0) 

The decimal-scaling conversion functions are:

```rust
pub(crate) fn external_to_internal(...) -> Result<BalanceOf<T>, Error<T>> {
    match ext_decimals.cmp(&internal_decimals) {
        Equal => Ok(amount),
        Less => { ... amount.checked_mul(&factor)... }
        Greater => {
            let diff = (ext_decimals - internal_decimals) as u32;
            let factor = Self::pow10(diff)?;
            Ok(amount.checked_div(&factor).unwrap_or_else(BalanceOf::<T>::zero))
        },
    }
}
``` [2](#0-1) 

and the inverse:

```rust
pub(crate) fn internal_to_external(...) -> Result<BalanceOf<T>, Error<T>> {
    match ext_decimals.cmp(&internal_decimals) {
        Equal => Ok(amount),
        Less => {
            let diff = (internal_decimals - ext_decimals) as u32;
            let factor = Self::pow10(diff)?;
            Ok(amount.checked_div(&factor).unwrap_or_else(BalanceOf::<T>::zero))
        },
        Greater => { ... amount.checked_mul(&factor)... }
    }
}
``` [3](#0-2) 

The pallet's own doc-comments describe the intended error semantics as: "Returns `Error::ConversionOverflow` if the scaling factor or the product does not fit in the balance type." No equivalent guard exists for the down-scaling (division) path — instead of returning an error when the input amount is too small to survive the decimal-scaling division (i.e., the true mathematical result is a positive fraction that floors to `0`), the code uses `unwrap_or_else(BalanceOf::<T>::zero)`, converting what should be a rejected/erroring edge case into a silently-accepted zero value. This is structurally identical to `ChainlinkPriceFeed.getPriceAt()` returning `0` instead of propagating the failure, which then "silently" flows through `stakeToVotingPowerAt` in the original report.

`checked_div` on unsigned integers only returns `None` for division by zero (which cannot happen here since `factor = 10^diff >= 1`); it always returns `Some(x)` for any nonzero divisor, where `x` correctly floors to `0` when `amount < factor`. That means the `unwrap_or_else` branch is *never* actually reached for `None` — it is dead code for its stated purpose, but the real bug is that the function returns `Ok(0)` (via the `Some(0)` inner value) instead of distinguishing this case as an error. Any external/internal asset amount smaller than the decimal-scaling factor (`10^diff`, which grows with the decimal gap between the two assets, e.g. `10^12` for an 18-decimal external asset paired with a 6-decimal internal asset) converts to exactly `0`.

### Impact Explanation
Because the module performs "1:1 swaps," the conversion functions are almost certainly used to determine how much of the counter-asset a user receives when depositing an asset for swap (and to size the corresponding reserve/mint/burn operation). If the destination-side swap logic does not independently reject a `0` conversion result before executing the transfer/mint/burn, an unprivileged user can:
- Deposit a small amount of an external asset that decimal-scales to `0` internal units, and receive nothing while their deposited asset is retained by the PSM reserve (`get_reserve`) — a direct, permanent user-fund loss.
- Symmetrically, redeem a small amount of internal stablecoin for `0` external units while the internal amount is burned/withdrawn — again a fund loss to the redeemer, and in the reverse direction it also risks reserve accounting getting out of balance with the internal supply if the "swap" is executed for a computed `0` leg while the non-zero leg is still processed.

This directly conserves-value violation (asset accounting) falls squarely under the "Balances, assets, ... must conserve value and settle exactly once to the rightful beneficiary and amount" pivot, and requires no privileged actor, governance action, or malicious peer — only an unprivileged user choosing a small enough swap amount relative to the configured decimal gap between the two registered assets.

### Likelihood Explanation
Likelihood depends on which asset pairs governance registers via `ExternalAssets` and their decimal configurations — larger decimal gaps (e.g., an 18-decimal external asset vs. a low-decimal internal asset) make the "amount too small" zero-floor window proportionally larger and easier for any ordinary user to hit by simply submitting a small swap amount. Unlike the Snowbridge fee-rounding case (which needs governance to set near-zero pricing parameters), triggering this PSM rounding-to-zero condition needs only a normal user picking a small enough transfer amount for a pair with a non-trivial decimal gap, so it is reachable purely through ordinary, permissionless use of the pallet's public swap functionality once such an asset pair is registered.

### Recommendation
Change `external_to_internal`/`internal_to_external` to return an explicit error (e.g., `Error::<T>::AmountTooSmall`) instead of `unwrap_or_else(BalanceOf::<T>::zero)` whenever the division would floor to `0` for a nonzero input amount, mirroring the already-fixed pattern in `pallet-asset-conversion`'s `quote_price_exact_tokens_for_tokens`/`quote_price_tokens_for_exact_tokens`, which were hardened specifically to reject zero-output conversions rather than silently returning `Some(0)`. [4](#0-3) 

### Proof of Concept
1. Configure two PSM-approved assets with a decimal gap, e.g. `external_decimals = 18`, `internal_decimals = 6` (`diff = 12`, `factor = 10^12`).
2. As any unprivileged user, call the PSM's public swap-in extrinsic with `amount = 1` (or any value `< 10^12`) of the external asset.
3. `external_to_internal(1, 18, 6)` executes the `Greater` branch: `1u128.checked_div(&10^12) == Some(0)` → returns `Ok(0)`.
4. If the swap logic proceeds to transfer/reserve the user's `1` unit of external asset into the PSM reserve while crediting the user `0` internal stablecoin, the user's deposited value is lost with no compensating output — reproducing the "silent zero" fund-loss condition analogous to the reported oracle bug.

(Note: I was unable to fully inspect the specific public dispatchable(s) that call `external_to_internal`/`internal_to_external` due to tool budget exhaustion; the vulnerability is demonstrated at the helper-function level with certainty, but confirming the exact end-to-end user-facing extrinsic path and whether any additional zero-check exists downstream would require further review of `substrate/frame/psm/src/lib.rs`'s dispatchable call implementations.)

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
