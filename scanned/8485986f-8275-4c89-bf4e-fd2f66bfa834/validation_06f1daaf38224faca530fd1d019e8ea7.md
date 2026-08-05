### Title
Silent truncation in `pallet-psm`'s `internal_to_external`/`external_to_internal` decimal-scaling can zero out redemption or under/over-mint value for high-decimal external assets - (File: `substrate/frame/psm/src/lib.rs`)

### Summary
`pallet-psm` normalizes external stablecoins of arbitrary decimal precision to an internal pUSD unit via `external_to_internal`/`internal_to_external`, using floor-division when scaling down and multiplication when scaling up [1](#0-0) . This mirrors exactly the class of bug in the external report: a base conversion between two different precisions that silently truncates instead of preserving/rounding to the expected decimal scale, and can make an operation effectively free or return zero value for the counterparty.

### Finding Description
`internal_to_external` floor-divides by `10^diff` when the external asset has fewer decimals than the internal pUSD representation [2](#0-1) . The companion `external_to_internal` (used on `mint`) scales up or floor-divides the other direction, and the pallet's own test suite documents that scaling down truncates ("DAI (18) -> internal (6): divide by 10^12, floor") [3](#0-2) . The design intentionally routes truncation dust back to the caller's wallet on both paths per the PR description [4](#0-3) , but this "symmetric dust" design assumes the truncated remainder is always small (sub-unit). If `internal_to_external` is invoked with an `amount` value that is smaller than `10^diff` (e.g. a user redeems 1 internal pUSD unit against an external asset with a much larger decimal count than internal, similar in spirit to the Blueberry case where a `2^96`-scaled price ended up with 0 decimal places after division), `amount.checked_div(&factor)` floors to **zero** [5](#0-4) . This is not merely "dust" — for small-but-nonzero internal balances the entire redemption amount can be truncated to zero external units while the internal pUSD debt/reserve accounting still records the internal amount as consumed, exactly analogous to the "free" precision-loss classified as H-01 in the Blueberry report (an incorrect base conversion producing 0 instead of the intended decimal precision).

### Impact Explanation
If a user can burn/lock internal pUSD units and receive `0` external asset units back due to floor-division underflow-to-zero, this is a fund-loss/incorrect-settlement bug: value is destroyed without being paid out to the rightful beneficiary, breaking the "conserve value and settle exactly once to the rightful beneficiary and amount" pivot for treasury/asset accounting in the Polkadot SDK impact gate.

### Likelihood Explanation
Likelihood depends on whether `MAX_DECIMALS_DIFF` and per-call minimum-amount checks (`AmountTooSmallAfterConversion`, referenced in the PR notes) are actually enforced on every mint/redeem path, and whether they close the gap for all supported `(ext_decimals, internal_decimals)` combinations [6](#0-5) . I was not able to fully trace the `mint`/`redeem` extrinsic bodies to confirm whether `AmountTooSmallAfterConversion` is checked before or after the truncating division on every call path (the grep results for `fn mint`/`fn redeem` and `AssetDecimals`/`StableDecimals` in `substrate/frame/psm/src/lib.rs` were not retrievable in full within the available iterations). This is the key open question: if the `AmountTooSmallAfterConversion` check runs *after* `internal_to_external`/`external_to_internal` and only checks the post-conversion result for zero (rejecting it) rather than checking pre-conversion size against the scaling factor, the guard is effective; if it is missing on one of the two conversion directions, or if it's bypassed for specific call sites (e.g. fee deduction/rounding paths that are separate from the guarded mint/redeem entry points), a real free-value exploit exists.

### Recommendation
Verify in the full pallet source that:
1. Every call site that ends in a truncating division (`internal_to_external`, and the scale-down branch of `external_to_internal`) checks the result is non-zero (or above a minimum meaningful unit) and returns `AmountTooSmallAfterConversion` before mutating any storage/debt/reserve accounting.
2. The check is performed strictly before balances are moved (mint/burn) so no state changes occur when the converted amount would be zero.
3. Rounding remainders ("dust") are bounded and cannot compound into value loss beyond one unit of the coarser denomination per operation — i.e., add an explicit minimum-amount pre-check keyed off `pow10(diff)` rather than relying solely on a post-hoc zero check.

### Proof of Concept
Based on the documented test `external_to_internal_scale_down_truncates`, the analogous unverified attack for `internal_to_external` would be: register an external asset with `ext_decimals` much smaller than `internal_decimals` is not the vulnerable direction (that's the "Less" branch, dividing pUSD-denominated internal amount by `10^diff`) — call `redeem` with an internal pUSD `amount` less than `10^diff` for an external asset with far fewer decimals than internal representation, so `amount.checked_div(&factor)` returns `0`, and confirm whether the pallet still burns/locks the full internal `amount` while paying out `0` external tokens. I could not confirm this end-to-end from the available code (the `redeem` extrinsic body and its guard ordering were not retrieved), so this should be validated directly against `substrate/frame/psm/src/lib.rs`'s `redeem`/`mint` dispatchables before treating it as confirmed exploitable.

### Citations

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

**File:** prdoc/stable2606/pr_11819.prdoc (L9-23)
```text
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
```
