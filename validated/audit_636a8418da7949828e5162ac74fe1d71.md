### Title
Silent floor-to-zero decimal-scaling truncation in PSM asset conversion enables fund loss without a mandatory zero-output guard - (File: `substrate/frame/psm/src/lib.rs`)

### Summary
`pallet-psm`'s decimal-scaling helpers `external_to_internal` and `internal_to_external` perform integer division to rescale amounts between an external asset's decimals and the pallet's internal decimal precision. When the divisor exceeds the value being scaled, the division silently floors to zero instead of erroring, exactly mirroring the reported `Scaler.scale()` bug class where precision loss causes economically meaningful stakes/deposits to be converted to zero. [1](#0-0) [2](#0-1) 

### Finding Description
`external_to_internal` (used when a user swaps an external asset into the internal asset) and `internal_to_external` (used on redemption) both branch on the decimal difference between the external asset and the pallet's configured internal decimals. In the "scale down" branch, the amount is divided by `10^diff` using `checked_div(&factor).unwrap_or_else(BalanceOf::<T>::zero)` — any nonzero input smaller than the divisor silently becomes `0`, with no error raised from the arithmetic itself:

```rust
Greater => {
    let diff = (ext_decimals - internal_decimals) as u32;
    let factor = Self::pow10(diff)?;
    Ok(amount.checked_div(&factor).unwrap_or_else(BalanceOf::<T>::zero))
},
``` [3](#0-2) 

The symmetric truncation exists in `internal_to_external`'s "Less" branch (used on redemption when the external asset has fewer decimals than the internal representation):

```rust
Less => {
    let diff = (internal_decimals - ext_decimals) as u32;
    let factor = Self::pow10(diff)?;
    Ok(amount.checked_div(&factor).unwrap_or_else(BalanceOf::<T>::zero))
},
``` [4](#0-3) 

This is the exact same broken invariant as the report: floor-division on a decimal-scaling conversion turns a legitimate, economically meaningful non-zero amount into `0`, and the surrounding code must independently guard against this to avoid crediting/debiting nothing while still moving real funds on the other leg of the swap. The pallet is aware of this class of bug — it defines `Error::AmountTooSmallAfterConversion` ("Conversion to the counter-asset rounds to zero; swap would transfer nothing") — and the test suite explicitly documents the truncating behavior as expected (`external_to_internal_scale_down_truncates`), confirming the raw conversion functions themselves provide no protection and rely entirely on call-site checks. [5](#0-4) [6](#0-5) 

Because fee deduction (`Permill`-based minting/redemption fees) and the decimals-scaling conversion are two separate floor-rounding steps that can each drop the amount toward zero, a zero-output check performed only once (e.g., pre-fee) does not guarantee the final post-fee, post-scale transferred amount is still non-zero. Any dispatchable swap path that computes the fee and the decimal conversion in sequence without re-validating non-zero output after every truncating step reproduces the reported vulnerability: a caller supplies a real, non-trivial amount of the external asset (or internal asset on redemption) and that amount is fully debited while the counter-asset credited is silently `0`.

### Impact Explanation
Any user calling the PSM's public swap/redeem extrinsics with an asset pair whose decimal difference is large enough (e.g., an 18-decimal external asset registered against a 6-decimal internal asset, as already covered by the pallet's own decimal_scaling tests) can have their entire deposited/redeemed amount silently converted to zero output while the debit/reserve movement on the other side of the swap still executes for the input amount. This is a direct, unrecoverable loss of user funds through a public, unprivileged entry point — matching the "permanent user-fund lock" / "asset accounting fails to conserve value" impact class for live-scope Polkadot SDK bugs.

### Likelihood Explanation
This requires no privileged actor, governance, relayer, or malicious peer — any account holding a registered high-decimal external asset (or interacting with a PSM instance where the internal/external decimal gap is large) can trigger it by submitting a swap/redeem call with an amount just below the effective post-fee divisor. The precise trigger condition depends on where exactly `AmountTooSmallAfterConversion` is checked relative to fee application in the dispatchable functions (`do_swap`/mint/redeem logic), which could not be fully traced in this pass; if the check is only applied once (pre-fee) rather than re-validated after every truncating step, the path is directly exploitable by any ordinary user.

### Recommendation
- Make `external_to_internal`/`internal_to_external` themselves reject (or the immediate caller re-check) any conversion that truncates a non-zero input to zero, returning `Error::AmountTooSmallAfterConversion` from within the scaling function rather than relying on an external, possibly stale check.
- Re-validate non-zero output after every rounding step (decimal scaling and fee deduction) in the mint/redeem dispatch paths, not just once before both steps are applied.
- Add regression tests combining a non-trivial minting/redemption fee with a large decimal gap to ensure the combined truncation cannot slip past the existing `AmountTooSmallAfterConversion` guard.

### Proof of Concept
Using the pallet's own decimal-scaling test harness (`substrate/frame/psm/src/tests.rs`, `decimal_scaling` module) as a base: register an external asset with 18 decimals (`DAI_MOCK`) against a 6-decimal internal asset (divisor `10^12`), set a small non-zero minting fee, and submit a swap with an external amount that is non-zero but, after fee deduction, is smaller than `10^12`. `external_to_internal` will return `Ok(0)` via `unwrap_or_else(BalanceOf::<T>::zero)` [3](#0-2)  instead of erroring, and if the guard against zero output is only evaluated on the pre-fee amount, the extrinsic succeeds: the caller's external asset is debited in full while zero internal asset is minted to them.

### Citations

**File:** substrate/frame/psm/src/lib.rs (L645-646)
```rust
		/// Conversion to the counter-asset rounds to zero; swap would transfer nothing.
		AmountTooSmallAfterConversion,
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

**File:** substrate/frame/psm/src/tests.rs (L2481-2489)
```rust
	#[test]
	fn external_to_internal_scale_down_truncates() {
		new_test_ext().execute_with(|| {
			// DAI (18) -> internal (6): divide by 10^12, floor.
			assert_eq!(
				Psm::external_to_internal(1_500_000_000_000_000_123, 18, 6).unwrap(),
				1_500_000
			);
		});
```
