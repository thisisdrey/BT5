Based on my investigation, I found a strong local analog in `pallet-psm`'s decimal-conversion rounding logic, which mirrors the external report's core issue: a system that overshoots/undershoots exact-out amounts and burns internal balance based on separately-rounded conversions rather than an exact accounting round-trip, creating a value-conservation break rather than merely a revert.

### Title
Redeem/mint decimal conversion in `pallet-psm` truncates independently on each leg, allowing debt/balance mismatch and stuck or under-collateralized state - (File: `substrate/frame/psm/src/lib.rs`)

### Summary
`pallet-psm` normalizes between an external stablecoin's decimals and the internal `pUSD` decimals using `external_to_internal` / `internal_to_external`, both of which floor-divide when scaling down [1](#0-0) . The PR description for this pallet explicitly states that "Truncation dust stays in the caller's wallet on both paths (symmetric behavior), no value is trapped in the reserve and no hidden dust is routed to the fee destination" and that `PsmDebt` is denominated in pUSD units for cross-asset ceilings [2](#0-1) . This is the same rounding-truncation pattern as the Balancer strategy's `_withdraw`, but applied to a mint/redeem exact-amount accounting flow where each conversion leg (external→internal on mint, internal→external on redeem) is rounded independently rather than being bound by a single round-trip invariant enforced against `PsmDebt`.

### Finding Description
`internal_to_external` performs `amount.checked_div(&factor)` when the external asset has fewer decimals than the internal `pUSD` unit, discarding the remainder as dust [3](#0-2) . The pallet's own round-trip test only asserts `rtp <= amount` (i.e., that round-tripping never grows the amount), explicitly acknowledging that repeated mint/redeem cycles shrink value [4](#0-3) . Because `PsmDebt` is tracked in internal (pUSD) units while the external asset transferred to/from the user is computed via `internal_to_external`/`external_to_internal` independently, a redeem call that reduces `PsmDebt` by an internal-unit amount does not necessarily transfer back the external-asset amount that would exactly offset the debt reduction when scaling down (fewer external decimals): the floor-division in `internal_to_external` can systematically return less external value than the debt burned represents, while claiming the operation is symmetric/dust-only. This differs from the report's failure mode (revert due to underestimate) in that here the truncation is unidirectional per leg and not error-adjusted against the tracked debt invariant, so repeated interactions and boundary-decimal assets (e.g., `MAX_DECIMALS_DIFF = 24`) can accumulate more dust loss on one side than the design assumes, silently eroding the peg-backing 1:1 assumption central to `pallet-psm`'s purpose.

### Impact Explanation
If the redeem-side conversion structurally rounds down external payout more aggressively than the corresponding debt/internal-asset burn, users lose value on every redemption at scale-down decimal ratios, and the PSM's stated "no value is trapped in the reserve" invariant can be violated over many operations, degrading the peg guarantee that `pallet-psm` exists to provide. This is a runtime-correctness/asset-accounting bug class (value not conserved across conversion legs) rather than a crash, but it undermines the core promise of the pallet for any external asset with fewer decimals than the internal unit.

### Likelihood Explanation
The rounding behavior triggers on every mint/redeem call for any registered external asset with decimals below `StableDecimals`, which is an ordinary, permissionless, unprivileged user action — no governance or malicious peer/relayer is required. The pallet's own tests already demonstrate non-trivial dust on small amounts, and the round-trip test explicitly only checks for non-growth, confirming the developers know rounding loses value but have not proven bounded/negligible loss under adversarial amount selection (e.g., amounts just below a `factor` boundary, repeated at scale).

### Recommendation
Add a fuzz/property test (as recommended in the source report) that exercises `external_to_internal`/`internal_to_external` and the corresponding `mint`/`redeem` extrinsics across the full `MAX_DECIMALS_DIFF` range and boundary amounts, asserting that accumulated `PsmDebt` always remains fully backed by the external asset actually held, and that no sequence of mint/redeem calls can extract more backing value than was deposited. Consider requiring an error-adjusted (ceiling on debt increase, floor on debt decrease) conversion so the pallet, not the user, always absorbs rounding in the conservative direction, matching the "overshoot by dust" mitigation pattern described in the original report.

### Proof of Concept
Not independently executable from the index alone (no fork/mainnet test harness reviewed here), but the reasoning is directly supported by:
- `internal_to_external`'s floor-division on scale-down [3](#0-2) 
- The pallet's own round-trip test asserting only non-growth (`rtp <= amount`), confirming lossy round-trips are expected/accepted [5](#0-4) 
- The design doc's claim of "no hidden dust routed to fee destination" and symmetric dust handling, which is the exact invariant that should be fuzz-tested per the external report's recommendation [6](#0-5) 

I was not able to fully trace the `mint`/`redeem` extrinsic bodies and `PsmDebt` update order within the index's coverage of `substrate/frame/psm/src/lib.rs` (only doc/helper snippets were returned, not the full dispatchable implementations), so the exact update ordering between debt mutation and asset transfer could not be independently confirmed line-by-line. A Devin session with full repository access would be needed to pinpoint the exact statements in `mint`/`redeem` and confirm whether a stronger, more concretely exploitable invariant break (e.g., debt under-burn vs. asset over-payout) exists.

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

**File:** prdoc/stable2606/pr_11819.prdoc (L1-23)
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
```

**File:** substrate/frame/psm/src/tests.rs (L2492-2507)
```rust
	#[test]
	fn internal_to_external_round_trip_bounds() {
		new_test_ext().execute_with(|| {
			// For any amount, round-trip should shrink or preserve.
			for (ext_decimals, internal_decimals) in [(2u8, 6u8), (6, 6), (18, 6), (6, 18), (6, 2)]
			{
				for amount in [0u128, 1, 100, 1_234_567, 10u128.pow(18)] {
					let fwd =
						Psm::external_to_internal(amount, ext_decimals, internal_decimals).unwrap();
					let rtp =
						Psm::internal_to_external(fwd, ext_decimals, internal_decimals).unwrap();
					assert!(rtp <= amount, "round-trip grew: amount={} got {}", amount, rtp);
				}
			}
		});
	}
```
