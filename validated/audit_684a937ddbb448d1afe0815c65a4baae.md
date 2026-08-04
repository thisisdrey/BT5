I have enough evidence from `substrate/frame/psm/src/lib.rs` to confirm the analog: the `mint`/`redeem` extrinsics only apply decimal-scaling conversion (`external_to_internal`/`internal_to_external`), with no price-oracle input anywhere in the pallet.

### Title
Peg Stability Module (`pallet-psm`) treats approved external stablecoins as always worth exactly 1 internal unit, with no depeg detection - ([File: substrate/frame/psm/src/lib.rs])

### Summary
`pallet-psm` implements a 1:1 swap between an internal stablecoin and approved external stablecoins (e.g. USDC/USDT) [1](#0-0) . The only value adjustment performed between the two assets is a decimal-precision rescale via `external_to_internal`/`internal_to_external`, which multiplies/divides by a power of 10 based on registered decimals — there is no USD-price oracle anywhere in the pallet [2](#0-1) . This is the same class of bug as the reported `MarketCore` issue: an on-chain safety/accounting system assumes a stablecoin trades exactly at its peg ($1 in the report, 1 internal-asset unit here) instead of consulting a price feed, so if any approved external stablecoin depegs, the pallet's accounting silently mis-prices it.

### Finding Description
`mint` computes `internal_equivalent = external_to_internal(external_amount, ext_decimals, internal_decimals)` — a pure decimal rescale, and mints that many internal tokens (minus fee) to the caller, while transferring the raw external amount into the PSM reserve [3](#0-2) . `redeem` does the symmetric operation, burning internal asset and paying out `external_out = internal_to_external(...)` [4](#0-3) . `PsmDebt` — the aggregate/per-asset debt ceiling used to bound how much internal asset can be backed by a given external — is denominated purely in internal units derived from this decimal conversion, never from a real-time USD value of the external asset [5](#0-4) . There is no oracle, no price check, and no depeg circuit breaker condition based on market price anywhere in `substrate/frame/psm/src/lib.rs` or `mock.rs` (grep for `oracle|price` returns only doc-comment mentions of the theoretical "price corridor" created by fees, not an actual price check) [6](#0-5) . The only guard against a bad external asset is a manually governance-set `CircuitBreakerLevel` (`AllEnabled`/`MintingDisabled`/`AllDisabled`) [7](#0-6) , which requires an admin to react after the fact — it is not an automatic, price-driven safety check like the `MarketCore` health/LTV calculation is supposed to be.

This exactly mirrors the reported unit-mismatch: `MarketCore` values collateral in USD but treats USG debt as if 1 USG == $1 without an oracle; `pallet-psm` values its reserve/debt purely in decimal-equivalent internal units and treats each approved external stablecoin as if 1 external-unit == 1 internal-unit == $1, with no oracle-based re-pricing.

### Impact Explanation
If any approved external stablecoin depegs downward (e.g. to $0.90), users can continue to `mint` at the stale 1:1 rate, depositing a devalued asset into the PSM reserve while receiving full-value internal stablecoin, silently under-collateralizing the PSM's aggregate `PsmDebt` and directly threatening the internal stablecoin's own peg/solvency — a chain-level runtime bug that compromises intended economic behavior (value-conservation invariant: the reserve should back internal-asset debt at real value, not nominal decimal-equivalent value). If the external stablecoin depegs upward, redeemers extract more USD value than they burned in internal asset, draining the reserve to the detriment of other internal-asset holders. Both cases reproduce the report's "silent insolvency" impact.

### Likelihood Explanation
Any unprivileged, signed account can call `mint`/`redeem` at any time; no attacker-controlled peer, relayer, validator, or governance action is required — only that a whitelisted external stablecoin depegs on the open market, which is a realistic and historically observed event (e.g. USDC's 2023 depeg). The circuit breaker is a manual/governance-reactive control, not a preventive check, so there is a window during which the exploit path is fully available to any user.

### Recommendation
Introduce a price-oracle input (e.g. a `PriceProvider`/`ConversionFromAssetBalance`-style trait as already used elsewhere in the codebase, such as `pallet-asset-rate`'s `ConversionFromAssetBalance`/`ConversionToAssetBalance`) to validate that the external asset's live price remains within an acceptable band of its peg before permitting `mint`, and to value `PsmDebt`/reserve backing in real USD terms rather than nominal decimal-equivalent units. Alternatively, add an automatic price-deviation circuit breaker that halts minting for an external asset once its oracle price departs from the peg band, rather than relying solely on manual governance intervention.

### Proof of Concept
1. Governance approves `USDX` as an external asset on the PSM with `weight = 100%`, decimals matching the internal asset (as in `register_external_asset_with_weight`/`set_zero_fees` used throughout `substrate/frame/psm/src/tests.rs`) [8](#0-7) .
2. Assume `USDX` depegs on secondary markets to $0.50 (no on-chain effect on the PSM, since it has no oracle).
3. Attacker calls `Psm::mint(origin, INTERNAL_ASSET_ID, USDX_ASSET_ID, 10_000 * USDX_UNIT, Permill::zero())`; per the `mint` logic [9](#0-8)  the pallet computes `internal_equivalent` via pure decimal rescale and mints the attacker 10,000 units of internal stablecoin, while the PSM reserve only holds $5,000 worth of real USDX value.
4. `PsmDebt` and reserve accounting show the PSM as fully backed (`psm_external_after == psm_debt_after`, as asserted in existing tests) [10](#0-9) , even though it is only 50% economically backed — silent insolvency exactly as described in the external report, now local to `pallet-psm`.

### Citations

**File:** substrate/frame/psm/src/lib.rs (L18-21)
```rust
//! # Peg Stability Module (PSM) Pallet
//!
//! Instantiable Peg Stability Modules (PSMs). Each PSM enables 1:1 swaps between an internal
//! stablecoin and one or more approved external stablecoins, typically to maintain a peg.
```

**File:** substrate/frame/psm/src/lib.rs (L42-50)
```rust
//! ## Overview
//!
//! A PSM strengthens its internal asset's peg by providing arbitrage opportunities:
//! - When the internal asset trades **above** $1: Users swap external assets for the internal asset
//!   and sell for profit.
//! - When the internal asset trades **below** $1: Users buy cheap internal asset and swap for
//!   external assets.
//!
//! This creates a price corridor bounded by the minting and redemption fees.
```

**File:** substrate/frame/psm/src/lib.rs (L716-756)
```rust
			let (ext_decimals, internal_decimals) =
				Self::ensure_decimals_match(&info, &internal_asset, &external_asset, &external)?;

			let internal_equivalent =
				Self::external_to_internal(external_amount, ext_decimals, internal_decimals)?;
			ensure!(!internal_equivalent.is_zero(), Error::<T>::AmountTooSmallAfterConversion);
			ensure!(internal_equivalent >= info.min_swap_amount, Error::<T>::BelowMinimumSwap);

			let effective_external =
				Self::internal_to_external(internal_equivalent, ext_decimals, internal_decimals)?;

			let fee_rate = MintingFee::<T>::get(&internal_asset, &external_asset);
			ensure!(fee_rate <= max_fee, Error::<T>::FeeTooHigh);
			let fee = fee_rate.mul_ceil(internal_equivalent);
			let internal_to_user = internal_equivalent.saturating_sub(fee);

			let current_total_psm_debt = Self::total_psm_debt(&internal_asset);
			ensure!(
				current_total_psm_debt.saturating_add(internal_equivalent) <= info.max_debt,
				Error::<T>::ExceedsMaxPsmDebt
			);

			let current_debt = PsmDebt::<T>::get(&internal_asset, &external_asset);
			let max_debt = Self::max_asset_debt(&internal_asset, &external_asset, &info);
			let new_debt = current_debt.saturating_add(internal_equivalent);
			ensure!(new_debt <= max_debt, Error::<T>::ExceedsMaxPsmDebt);

			let psm_account = Self::psm_account(&internal_asset);
			T::Fungibles::transfer(
				external_asset.clone(),
				&who,
				&psm_account,
				effective_external,
				Preservation::Expendable,
			)?;
			T::Fungibles::mint_into(internal_asset.clone(), &who, internal_to_user)?;
			if !fee.is_zero() {
				T::Fungibles::mint_into(internal_asset.clone(), &info.fee_destination, fee)?;
			}

			PsmDebt::<T>::insert(&internal_asset, &external_asset, new_debt);
```

**File:** substrate/frame/psm/src/lib.rs (L825-887)
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

			let current_debt = PsmDebt::<T>::get(&internal_asset, &external_asset);
			ensure!(current_debt >= effective_internal_net, Error::<T>::InsufficientReserve);

			let reserve = Self::get_reserve(&internal_asset, &external_asset);
			if reserve < external_out {
				defensive!("PSM reserve is less than expected output amount");
				return Err(Error::<T>::Unexpected.into());
			}

			if !fee.is_zero() {
				T::Fungibles::transfer(
					internal_asset.clone(),
					&who,
					&info.fee_destination,
					fee,
					Preservation::Expendable,
				)?;
			}

			if !effective_internal_net.is_zero() {
				T::Fungibles::burn_from(
					internal_asset.clone(),
					&who,
					effective_internal_net,
					Preservation::Expendable,
					Precision::Exact,
					Fortitude::Polite,
				)?;
			}

			let psm_account = Self::psm_account(&internal_asset);
			if !external_out.is_zero() {
				T::Fungibles::transfer(
					external_asset.clone(),
					&psm_account,
					&who,
					external_out,
					Preservation::Expendable,
				)?;
			}
```

**File:** substrate/frame/psm/src/lib.rs (L1575-1598)
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
```

**File:** substrate/frame/psm/README.md (L65-75)
```markdown
## Debt Ceiling

Each PSM instance has an absolute internal-asset debt ceiling stored on
`PsmInfo::max_debt`. Within that, per-external ceilings are derived from
ceiling weights:

```
max_asset_debt(internal, external) =
    (AssetCeilingWeight[internal, external] / sum_of_weights[internal])
        * Psm[internal].max_debt
```
```

**File:** substrate/frame/psm/README.md (L93-105)
```markdown
## Circuit Breaker

Each approved external on each instance has an independent circuit breaker
with three levels:

| Level             | Minting | Redemption | Use Case                          |
| ----------------- | ------- | ---------- | --------------------------------- |
| `AllEnabled`      | Allowed | Allowed    | Normal operation                  |
| `MintingDisabled` | Blocked | Allowed    | Drain debt from a problematic external |
| `AllDisabled`     | Blocked | Blocked    | Full emergency halt of an external |

`set_asset_status` is callable at both the `Full` (`full_admin`) and
`Emergency` (`emergency_admin`) levels.
```

**File:** substrate/frame/psm/src/tests.rs (L2374-2379)
```rust
			// Assertions
			assert!(cycle > 0, "Should have completed at least one cycle");
			assert_eq!(if_increase, total_fees, "IF should receive all fees");
			assert_eq!(psm_external_after, psm_debt_after, "PSM external = PSM debt");
			assert_eq!(user_decrease, total_fees, "User loss equals fees");
			assert!(psm_debt_after <= max_debt, "PSM debt should not exceed ceiling");
```

**File:** substrate/frame/psm/src/tests.rs (L2818-2832)
```rust
	#[test]
	fn redeem_scale_down_usdx_dust_stays_with_user() {
		new_test_ext().execute_with(|| {
			register_external_asset_with_weight(USDX_ASSET_ID, Permill::from_percent(100));
			set_zero_fees(USDX_ASSET_ID);

			// Mint first so PSM has reserve. 10_000 USDX -> 10_000 internal debt.
			let usdx_raw = 10_000 * USDX_UNIT;
			assert_ok!(Psm::mint(
				RuntimeOrigin::signed(ALICE),
				INTERNAL_ASSET_ID,
				USDX_ASSET_ID,
				usdx_raw,
				Permill::zero()
			));
```
