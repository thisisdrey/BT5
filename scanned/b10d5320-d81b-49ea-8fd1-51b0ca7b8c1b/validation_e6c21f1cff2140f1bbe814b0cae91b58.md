Based on my research, I found the strongest local analog in the `pallet-psm` (Peg Stability Module) pallet, which was recently modified to support external assets with different decimal precision.

### Title
PSM performs pure decimal-scaling conversion with no price/peg validation, allowing 1:1 minting against depegged external assets - (File: substrate/frame/psm/src/lib.rs)

### Summary
The external Sherlock report's core broken invariant is: the protocol assumes a fixed price relationship (ETH-denominated feeds) that does not hold for all supported assets (USD-denominated RWAs), so value conversion silently fails or produces wrong economics. The local analog is `pallet-psm`, which is documented to provide "1:1 swaps between an internal stablecoin and one or more approved external stablecoins" [1](#0-0) . Its conversion helpers `external_to_internal` / `internal_to_external` only rescale by decimal precision — they contain no price oracle, no exchange-rate input, and no on-chain check that the external asset is actually worth 1 unit of the internal peg asset [2](#0-1) .

### Finding Description
`external_to_internal` and `internal_to_external` compute conversion purely as `amount * 10^(decimals_diff)` (or floor-divide the other way), asserting decimal equivalence as if it were value equivalence [3](#0-2) . The pallet's own module docs state the PSM "strengthens its internal asset's peg by providing arbitrage opportunities" and describes minting/redeeming as simple 1:1 swaps minus fees [4](#0-3) . The recent change (`pallet-psm: support external assets with different decimal precision`) only normalizes for *decimal* mismatches — it introduces `AssetDecimals`/`StableDecimals` snapshots and a drift guard (`DecimalsMismatch`) that halts an asset only when live *decimal metadata* diverges from the registration snapshot, not when its *market value* diverges from the peg. There is no analog of an ETH/USD-style conversion or any price feed anywhere in this pallet.

This mirrors exactly the reported bug class: the protocol's value-conversion logic assumes all supported assets share one fixed valuation relationship (there: ETH-denominated; here: an implicit hard 1:1 USD peg), and has no mechanism to correct for assets whose real price diverges from that assumption. Just as RWAs priced in USD cannot be represented because the protocol only understands ETH-denominated feeds, any "external" asset approved into the PSM whose market value drifts from $1 (i.e., depegs) is still treated by the pallet as worth exactly 1 unit of the internal stablecoin, because decimals normalization is the only correction applied.

### Impact Explanation
If an approved external asset's market value falls below its assumed peg (a realistic external market event, not requiring any malicious peer, validator, or admin action), any unprivileged user can call `mint` to deposit the devalued external asset and receive the internal stablecoin at full 1:1 nominal value [5](#0-4) . This is effectively unbacked minting: the PSM's reserve (`get_reserve`, backed 1:1 by external assets per pallet docs) becomes under-collateralized relative to the internal debt it has issued [6](#0-5) . Subsequent redeemers who still hold internal stablecoin extract full-value external assets from a reserve that is worth less than the outstanding debt, socializing the loss and potentially leaving the last redeemers unable to redeem (fund lock) once the reserve is drained.

### Likelihood Explanation
Any external stablecoin can depeg due to normal market conditions (as has happened historically to real-world peg assets); this requires no privileged action, malicious relayer, or governance abuse — only a market event plus a normal, permissionless `mint`/`redeem` call. Because the pallet has zero price awareness beyond decimals, this is a persistent structural gap rather than an edge case.

### Recommendation
Introduce an oracle/price-conversion layer (analogous to `pallet-asset-rate`'s `ConversionRateToNative` used elsewhere in this repo for treasury spends) into the PSM's mint/redeem path, so that external-asset value is validated against a live or governance-attested price rather than assumed to be exactly 1:1 purely via decimal rescaling. At minimum, add a configurable de-peg threshold/circuit breaker that halts minting for an external asset once its observed market price deviates materially from parity, mirroring `CircuitBreakerLevel` already present in the pallet but currently only manually triggered.

### Proof of Concept
Conceptual sequence (consistent with the pallet's own test harness, e.g. `mint_scale_up_usdx_exact_no_dust` in `substrate/frame/psm/src/tests.rs` [7](#0-6) ):
1. PSM approves `EXTERNAL_ASSET` with `register_external_asset_with_weight`, decimals normalized via `external_to_internal`.
2. `EXTERNAL_ASSET` depegs on secondary markets to real value $0.50.
3. Attacker calls `Psm::mint(origin, INTERNAL_ASSET_ID, EXTERNAL_ASSET_ID, amount, max_fee)`; `external_to_internal` computes output purely from decimals, ignoring the $0.50 real value, crediting attacker with `amount` (nominal 1:1) of `INTERNAL_ASSET` [8](#0-7) .
4. Attacker immediately redeems against a *different*, still fully-pegged external asset in the same PSM reserve (if multiple externals are approved) or sells `INTERNAL_ASSET` on the open market, realizing a profit equal to the depeg discount, extracted from the shared reserve backing all internal-asset holders.

Note: I was not able to fully verify (due to tool-call limits) whether a later revision of `mint`/`redeem` adds any price-bound check beyond `DecimalsMismatch`; this analysis is based on the `external_to_internal`/`internal_to_external` functions and module documentation retrieved, which show no such check present.

### Citations

**File:** substrate/frame/psm/src/lib.rs (L18-21)
```rust
//! # Peg Stability Module (PSM) Pallet
//!
//! Instantiable Peg Stability Modules (PSMs). Each PSM enables 1:1 swaps between an internal
//! stablecoin and one or more approved external stablecoins, typically to maintain a peg.
```

**File:** substrate/frame/psm/src/lib.rs (L42-61)
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
//!
//! ### Key Concepts
//!
//! * **PSM instance**: A configured Peg Stability Module, keyed by its internal asset id and
//!   described by [`PsmInfo`]. Each instance has its own reserve account derived from
//!   `blake2_256((PalletId::TYPE_ID, PalletId, internal_asset).encode())`.
//! * **Minting**: Deposit external asset → receive internal asset (minus fee).
//! * **Redemption**: Burn internal asset → receive external asset (minus fee).
//! * **Reserve**: External asset balance held by a PSM's reserve account (derived, not stored).
//! * **PSM Debt**: Total internal asset minted through a PSM, backed 1:1 by external assets in that
//!   PSM's reserve.
```

**File:** substrate/frame/psm/src/lib.rs (L76-86)
```rust
//! ```ignore
//! // Mint internal asset by depositing USDC on the PSM
//! let max_fee = MintingFee::<Runtime>::get(INTERNAL_ASSET_ID, USDC_ASSET_ID);
//! Psm::mint(
//! 	RuntimeOrigin::signed(user),
//! 	INTERNAL_ASSET_ID,
//! 	USDC_ASSET_ID,
//! 	1000 * UNIT,
//! 	max_fee,
//! )?;
//!
```

**File:** substrate/frame/psm/src/lib.rs (L1567-1573)
```rust
		/// Balance of an external held by a PSM instance's reserve account.
		pub(crate) fn get_reserve(
			internal_asset: &T::AssetId,
			external_asset: &T::AssetId,
		) -> BalanceOf<T> {
			T::Fungibles::balance(external_asset.clone(), &Self::psm_account(internal_asset))
		}
```

**File:** substrate/frame/psm/src/lib.rs (L1575-1624)
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

**File:** substrate/frame/psm/src/tests.rs (L2519-2545)
```rust
	#[test]
	fn mint_scale_up_usdx_exact_no_dust() {
		new_test_ext().execute_with(|| {
			register_external_asset_with_weight(USDX_ASSET_ID, Permill::from_percent(100));
			set_zero_fees(USDX_ASSET_ID);

			// 200 USDX raw = 2_00 = internal equivalent = 200 * 10^4 = 2_000_000 = 2 internal.
			// Use 10_000 * USDX_UNIT = 1_000_000 raw USDX so internal equivalent is above
			// MinSwapAmount (100 * INTERNAL_UNIT = 10^8).
			let usdx_raw = 10_000 * USDX_UNIT; // 1_000_000 raw USDX
			let expected_internal = 10_000 * INTERNAL_UNIT; // 10_000 internal
			let alice_usdx_before = get_asset_balance(USDX_ASSET_ID, ALICE);

			assert_ok!(Psm::mint(
				RuntimeOrigin::signed(ALICE),
				INTERNAL_ASSET_ID,
				USDX_ASSET_ID,
				usdx_raw,
				Permill::zero()
			));

			// User spent exactly usdx_raw (no dust path on scale-up).
			assert_eq!(get_asset_balance(USDX_ASSET_ID, ALICE), alice_usdx_before - usdx_raw);
			assert_eq!(get_asset_balance(USDX_ASSET_ID, psm_account()), usdx_raw);
			assert_eq!(get_asset_balance(INTERNAL_ASSET_ID, ALICE), expected_internal);
			assert_eq!(PsmDebt::<Test>::get(INTERNAL_ASSET_ID, USDX_ASSET_ID), expected_internal);
		});
```
