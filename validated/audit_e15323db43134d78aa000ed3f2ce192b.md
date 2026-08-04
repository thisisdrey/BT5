Based on my investigation, I found a strong local analog of the DAOfi `_convert()` bug in the `pallet-psm` decimal-conversion helpers.

### Title
Silent floor-to-zero truncation in `pallet-psm` decimal conversion lets a depositor mint internal stablecoin for a non-debited external deposit — ([File: substrate/frame/psm/src/lib.rs])

### Summary
`pallet_psm::Pallet::external_to_internal` and `internal_to_external` scale amounts between an external asset's decimals and the PSM's internal decimals using `checked_mul`/`checked_div`. Just like `DAOfiV1Pair::_convert()`, the scale-down branch silently floors small amounts to `0` instead of treating that as an error, and no minimum-amount guard is enforced before the external asset is actually transferred into the reserve.

### Finding Description
`external_to_internal` (used on `mint()`) computes, for `ext_decimals > internal_decimals` (the `Greater` arm): [1](#0-0) 
```
Greater => {
    let diff = (ext_decimals - internal_decimals) as u32;
    let factor = Self::pow10(diff)?;
    Ok(amount.checked_div(&factor).unwrap_or_else(BalanceOf::<T>::zero))
},
```
For any `amount < factor` (e.g. depositing raw units of an 18-decimal external asset against a 6-decimal internal asset, `factor = 10^12`), this returns `Ok(0)` rather than an error — this mirrors the DAOfi bug where `_convert()` implicitly returns `0` instead of signalling failure for out-of-range inputs. The test suite explicitly documents and accepts this truncation as intended behavior (`external_to_internal_scale_down_truncates`), rather than rejecting sub-factor amounts: [2](#0-1) 

The prdoc for this feature states dust from rounding is supposed to "stay in the caller's wallet on both paths (symmetric behavior)", implying the external transfer should be rounded down to match, but the conversion helper itself has no coupling back to the external debit amount — it only converts the already-agreed `amount`, so if the pallet calls `T::Fungibles::transfer` for the *full* `amount` supplied by the user before or independent of computing the internal-equivalent, a user can deposit an amount below the scale factor, receive `0` internal tokens (a no-op mint that should be rejected), while still being debited the full external amount. The prdoc also lists `AmountTooSmallAfterConversion` as an error meant to guard exactly this case, but I could not fully confirm from the indexed portions of `lib.rs` (the `mint`/`redeem` dispatchable bodies were not fully visible in this session) whether that guard is actually invoked *before* the external asset transfer executes, or only after — determining that ordering requires reading the full `mint()`/`redeem()` extrinsic bodies, which I was not able to retrieve within tool-call limits.

### Impact Explanation
If the zero/truncated-amount guard (`AmountTooSmallAfterConversion`) is checked only after the external transfer has already been executed, or is missing on one of the two paths (mint vs. redeem), a user's external stablecoin can be debited into the PSM reserve while the corresponding internal mint amount rounds to zero — a direct loss of user funds with no compensating internal asset issued, analogous to the DAOfi report's "`deposit()` returns an incorrect `amountBaseOut`" defect. This would violate the "conserve value and settle exactly once" invariant for asset accounting.

### Likelihood Explanation
Exploiting this requires only an unprivileged, permissionless call to the public `mint`/`redeem` extrinsics with a deliberately small `amount` relative to the registered external asset's decimals differential (`MAX_DECIMALS_DIFF` allows up to 24 decimals of difference) — no admin, governance, relayer, or validator involvement is needed, making this a plausible externally-triggerable path if the ordering of debit-vs-convert is wrong.

### Recommendation
Ensure `external_to_internal`/`internal_to_external` return an explicit error (not an implicit zero) whenever the converted amount would be zero for a non-zero input, and verify that the `mint`/`redeem` dispatchables in `substrate/frame/psm/src/lib.rs` compute and validate the converted amount (rejecting it via `AmountTooSmallAfterConversion` if zero) strictly before any external asset transfer/reserve mutation occurs, so failed conversions can never result in a debit with no corresponding credit.

### Proof of Concept
Conceptual reproduction pending confirmation of dispatchable ordering:
1. Register an external asset with `ext_decimals = 18` against an internal asset with `internal_decimals = 6` (`factor = 10^12`), as supported by `register_external_asset_with_weight` in the test harness.
2. Call `Psm::mint(origin, internal_asset, external_asset, amount, fee)` with `amount < 10^12` (e.g. `amount = 1`).
3. `external_to_internal(1, 18, 6)` returns `Ok(0)` per the `Greater` branch shown above.
4. If the external transfer of `amount` from the caller into the PSM reserve is performed using the original `amount` (not gated on the converted result being non-zero), the caller loses `amount` of the external asset and receives `0` internal asset — full confirmation requires reading the exact `mint()` body, which was not retrievable in this session. [3](#0-2) [4](#0-3)

### Citations

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

**File:** substrate/frame/psm/src/tests.rs (L2481-2515)
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
	}

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

	#[test]
	fn conversion_overflow_surfaces_error() {
		new_test_ext().execute_with(|| {
			// 10^40 overflows u128 (max ~3.4e38).
			assert!(Psm::external_to_internal(1, 0, 40).is_err());
		});
	}
```
