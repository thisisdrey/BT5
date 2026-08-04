This confirms the analog: `pallet-psm`'s `mint`/`redeem` functions perform only decimal-scaling conversion (`external_to_internal`/`internal_to_external`) between the internal stablecoin and any approved external asset — there is no price oracle, no peg-verification, and no `price`/`Oracle` reference anywhere in the pallet [1](#0-0) . This is the same unit-mismatch class as the USG report: the code assumes 1 external unit == 1 internal unit == $1, with only a circuit breaker (manual governance action) as a backstop [2](#0-1) .

### Title
PSM mint/redeem trusts hardcoded 1:1 par value between internal stablecoin and external assets with no oracle price check, enabling insolvency draining on depeg - (File: `substrate/frame/psm/src/lib.rs`)

### Summary
`pallet-psm`'s `mint` and `redeem` extrinsics convert between the internal stablecoin and any approved external stablecoin using only decimal-precision scaling (`external_to_internal` / `internal_to_external`), never consulting any price oracle to confirm the external asset is actually worth $1 [3](#0-2) . This mirrors the reported USG bug class: the code hardcodes a 1:1 par-value assumption between two independently priced tokens instead of converting via real USD value, and relies solely on human/governance-triggered circuit breakers to react after the fact [2](#0-1) .

### Finding Description
In `mint` (`substrate/frame/psm/src/lib.rs` lines 700-767), the external asset amount deposited by the user is converted to internal units purely through `external_to_internal`, which only rescales by `10^(ext_decimals - internal_decimals)` — a pure decimal-precision adjustment, not a value conversion [4](#0-3) . The resulting `internal_equivalent` is minted 1:1 to the user (minus fee) and added to `PsmDebt`, which the pallet documentation and code explicitly describe as "backed 1:1 by external assets in that PSM's reserve" [5](#0-4) . Symmetrically, `redeem` burns internal tokens and pays out the external asset using the same decimal-only conversion, with no reference to actual market value [6](#0-5) .

There is no oracle integration, price feed, or peg-verification check anywhere in the pallet — a grep across the file for `price`/`oracle` returns no functional matches [7](#0-6) . The only safety mechanism is the `CircuitBreakerLevel` (`AllEnabled`/`MintingDisabled`/`AllDisabled`), which is a manually-set admin/governance control, not an automatic on-chain response to a depeg [8](#0-7) . If any approved external asset (e.g. USDC-equivalent) depegs below $1 on secondary markets, the pallet will still mint the internal stablecoin at full 1:1 par against the depegged collateral, exactly matching the "assumes 1 token == $1" flaw described in the external USG report, and unlike the USG `Collateral.sol` case, this on-chain logic is a public, permissionless entrypoint (`mint`), not merely an internal risk-check function.

### Impact Explanation
An attacker (or any user) can mint the internal stablecoin at full par value using a depegged/worthless external asset before the circuit breaker is manually triggered by governance, directly diluting/undercollateralizing the PSM's aggregate internal-asset backing. Because `PsmDebt` and reserve balances span multiple external assets sharing one internal-asset supply, this newly minted (unbacked) internal stablecoin can then be redeemed via `redeem` against a *different*, healthy external asset in the same PSM instance, effectively draining good collateral from redeemers of the healthy pair and causing insolvency of the PSM instance — a direct "theft or unbacked mint" impact matching the accepted impact gate.

### Likelihood Explanation
Likelihood is elevated because: (1) `mint`/`redeem` are public, unprivileged extrinsics reachable by any signed account, (2) the ceiling/weight checks (`max_debt`, `AssetCeilingWeight`) only limit total exposure, they do not detect or price depegs, and (3) reaction depends entirely on governance/admin noticing the depeg and calling `set_asset_status` — a real-world, non-trivial time lag during which arbitrage against the peg assumption is fully exploitable, as acknowledged for the analogous USG issue.

### Recommendation
Introduce an oracle-based value check (e.g., via `pallet-oracle`, already present in this repository, or a `ConversionToAssetBalance`/`ConversionFromAssetBalance` price adapter as used elsewhere in the codebase such as `pallet-asset-rate`) so that `mint`/`redeem` compute `internal_equivalent` based on the external asset's live USD price rather than a pure decimal rescale, and/or add an automatic on-chain deviation guard that halts minting when the observed price of an external asset strays outside a configured band around $1, rather than relying solely on manual circuit-breaker intervention.

### Proof of Concept
1. PSM instance is configured with internal asset `pUSD` and two external assets, `USDX` (healthy) and `USDY` (about to depeg), both with `AllEnabled` circuit breaker status and nonzero `AssetCeilingWeight`.
2. `USDY` depegs on secondary markets to $0.10, but its `CircuitBreakerLevel` remains `AllEnabled` (governance has not yet reacted).
3. Attacker calls `Psm::mint(origin, pUSD, USDY, large_amount, max_fee)`. `external_to_internal` only rescales decimals — it mints `pUSD` at full par against `USDY`, ignoring the real $0.10 value (see conversion logic at `substrate/frame/psm/src/lib.rs` lines 716-725).
4. Attacker calls `Psm::redeem(origin, pUSD, USDX, internal_amount, max_fee)` to redeem the freshly minted (unbacked) `pUSD` for the healthy `USDX` reserve, per the redeem logic at lines 835-846.
5. The PSM instance's `USDX` reserve is drained relative to its tracked `PsmDebt`, leaving legitimate `USDX` depositors unable to redeem — insolvency realized, matching the "silent insolvency" impact of the external USG report.

### Citations

**File:** substrate/frame/psm/src/lib.rs (L1-150)
```rust
// This file is part of Substrate.

// Copyright (C) Amforc AG.
// SPDX-License-Identifier: Apache-2.0

// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
// 	http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

//! # Peg Stability Module (PSM) Pallet
//!
//! Instantiable Peg Stability Modules (PSMs). Each PSM enables 1:1 swaps between an internal
//! stablecoin and one or more approved external stablecoins, typically to maintain a peg.
//!
//! ## Pallet API
//!
//! See the [`pallet`] module for more information about the interfaces this pallet exposes,
//! including its configuration trait, dispatchables, storage items, events and errors.
//!
//! ## Terminology
//!
//! Throughout this pallet two distinct token roles are referenced:
//!
//! * **Internal** — the stablecoin a PSM issues and burns (e.g. a runtime's own USD-pegged
//!   stablecoin). Each PSM instance is keyed by its internal asset id; multiple instances can
//!   coexist, each with its own reserve, debt ceiling, fee destination and approved externals. Mint
//!   operations credit the user with the internal asset; redeem operations burn it. Fees are
//!   collected in the internal asset and forwarded to that instance's [`PsmInfo::fee_destination`].
//! * **External** — third-party assets (e.g. USDC, USDT) approved on a specific PSM via
//!   [`Pallet::add_external_asset`] and held in that PSM's reserve. Users deposit external to mint
//!   internal, and burn internal to redeem external. A PSM may approve multiple externals, each
//!   identified by `external_asset`.
//!
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
//! * **Circuit Breaker**: Per-external emergency control to disable minting or all swaps.
//!
//! ### Fee Structure
//!
//! * **Minting Fee (`MintingFee`)**: Deducted from internal-asset output during minting, configured
//!   per `(internal_asset, external_asset)` pair.
//! * **Redemption Fee (`RedemptionFee`)**: Deducted from external-asset output during redemption,
//!   configured per `(internal_asset, external_asset)` pair.
//!
//! Fees are collected in the internal asset and transferred to the instance's
//! [`PsmInfo::fee_destination`].
//!
//! ### Example
//!
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
//! // Redeem USDC by burning the internal asset
//! let max_fee = RedemptionFee::<Runtime>::get(INTERNAL_ASSET_ID, USDC_ASSET_ID);
//! Psm::redeem(
//! 	RuntimeOrigin::signed(user),
//! 	INTERNAL_ASSET_ID,
//! 	USDC_ASSET_ID,
//! 	1000 * UNIT,
//! 	max_fee,
//! )?;
//! ```

#![cfg_attr(not(feature = "std"), no_std)]

extern crate alloc;

pub mod weights;

#[cfg(feature = "runtime-benchmarks")]
mod benchmarking;
#[cfg(test)]
mod mock;
#[cfg(test)]
mod tests;

pub use pallet::*;
pub use weights::WeightInfo;

/// Helper trait for benchmark setup.
///
/// Provides a way to create an external asset with the correct metadata (decimals)
/// for benchmarks, abstracting over the deposit requirements of the underlying
/// asset pallet.
#[cfg(feature = "runtime-benchmarks")]
pub trait BenchmarkHelper<AssetId, AccountId> {
	/// Get the asset ID for a given asset index.
	fn get_asset_id(asset_index: u32) -> AssetId;
	/// Create an asset with metadata matching the internal asset's decimals.
	fn create_asset(asset_id: AssetId, owner: &AccountId, decimals: u8);
}

#[frame_support::pallet]
pub mod pallet {

	use alloc::boxed::Box;
	use codec::DecodeWithMemTracking;
	use frame_support::{
		pallet_prelude::*,
		traits::{
			fungibles::{
				metadata::Inspect as FungiblesMetadataInspect,
				roles::Inspect as FungiblesRolesInspect, Inspect as FungiblesInspect,
				Mutate as FungiblesMutate,
			},
			tokens::{Fortitude, Precision, Preservation},
			CallerTrait, Consideration, EnsureOriginWithArg, Footprint, OriginTrait,
		},
		PalletId,
	};
	use frame_system::pallet_prelude::*;
	use sp_runtime::{
		traits::{CheckedDiv, CheckedMul, Saturating, TrailingZeroInput, Zero},
		Perbill, Permill, TypeId,
	};

```

**File:** substrate/frame/psm/src/lib.rs (L153-187)
```rust
	/// Circuit breaker levels for emergency control.
	#[derive(
		Encode,
		Decode,
		DecodeWithMemTracking,
		MaxEncodedLen,
		TypeInfo,
		Clone,
		Copy,
		PartialEq,
		Eq,
		Debug,
		Default,
	)]
	pub enum CircuitBreakerLevel {
		/// Normal operation, all swaps enabled.
		#[default]
		AllEnabled,
		/// Minting disabled, redemptions still allowed.
		MintingDisabled,
		/// All swaps disabled.
		AllDisabled,
	}

	impl CircuitBreakerLevel {
		/// Whether this level allows minting (external → internal).
		pub const fn allows_minting(&self) -> bool {
			matches!(self, CircuitBreakerLevel::AllEnabled)
		}

		/// Whether this level allows redemption (internal → external).
		pub const fn allows_redemption(&self) -> bool {
			!matches!(self, CircuitBreakerLevel::AllDisabled)
		}
	}
```

**File:** substrate/frame/psm/src/lib.rs (L716-725)
```rust
			let (ext_decimals, internal_decimals) =
				Self::ensure_decimals_match(&info, &internal_asset, &external_asset, &external)?;

			let internal_equivalent =
				Self::external_to_internal(external_amount, ext_decimals, internal_decimals)?;
			ensure!(!internal_equivalent.is_zero(), Error::<T>::AmountTooSmallAfterConversion);
			ensure!(internal_equivalent >= info.min_swap_amount, Error::<T>::BelowMinimumSwap);

			let effective_external =
				Self::internal_to_external(internal_equivalent, ext_decimals, internal_decimals)?;
```

**File:** substrate/frame/psm/src/lib.rs (L835-846)
```rust
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
