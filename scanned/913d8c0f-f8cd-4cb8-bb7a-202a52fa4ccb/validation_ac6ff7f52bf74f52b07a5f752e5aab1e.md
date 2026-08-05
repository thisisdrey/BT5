### Title
PSM mint/redeem treats external stablecoins as always worth $1, allowing depegged-asset arbitrage to under-collateralize the internal stablecoin - ([File: substrate/frame/psm/src/lib.rs])

### Summary
The `psm` (Peg Stability Module) pallet performs 1:1 swaps between an internal stablecoin and approved external stablecoins (e.g. USDC, USDT) with no on-chain price oracle. This is the direct local analog of the external report's "under-peg" bug: just as `getAccruedYield` summed asset balances without checking whether each asset was trading below $1, the PSM's mint/redeem/debt-accounting logic sums and converts external-asset balances 1:1 against the internal asset with no reference to real market price.

### Finding Description
The pallet doc explicitly states its purpose: "Instantiable Peg Stability Modules (PSMs)... enable 1:1 swaps between an internal stablecoin and one or more approved external stablecoins" [1](#0-0) . `PsmDebt` is described as "Total internal asset minted through a PSM, backed 1:1 by external assets in that PSM's reserve" [2](#0-1) .

The only conversion performed between internal and external units is a decimal-scaling operation, `external_to_internal`, which scales purely by `10^(ext_decimals - internal_decimals)` — it has no price/rate input at all: [3](#0-2) . There is no `ConversionRateToNative`-style oracle lookup (as used elsewhere in the codebase, e.g. `pallet-asset-rate`'s `to_asset_balance`/`from_asset_balance` [4](#0-3) ) anywhere in the PSM's mint/redeem/reserve-accounting path.

Because the PSM assumes every approved external asset is always worth exactly $1 relative to the internal stablecoin, `mint` will credit a user with `internal_asset` 1:1 (minus a fixed fee) for external tokens that have actually depegged below $1. This directly mirrors the reported class of bug: an accounting routine (`getAccruedYield` in the report, `mint`/`redeem`/`PsmDebt` accounting here) that sums/attributes value across multiple assets without accounting for the possibility that an asset's real-world price is below its nominal 1:1 assumption.

### Impact Explanation
If any approved external asset depegs below $1 (a realistic stablecoin failure mode, as seen historically with USDC/USDT depegs), an attacker (any unprivileged user) can:
1. Buy the depegged external asset cheaply off-chain/on a DEX.
2. Call `mint` on the PSM to swap the discounted external asset 1:1 (minus the fixed `MintingFee`) for the internal stablecoin, which the rest of the system treats as fully backed and worth $1.
3. Sell/use the freshly minted internal stablecoin at its full nominal value elsewhere.

This directly under-collateralizes the internal stablecoin: `PsmDebt` records the internal tokens as fully backed 1:1 by external reserve, but the external reserve's real market value is below the recorded internal-asset liability. This is unbacked/undercollateralized minting — a live-scope impact ("theft or unbacked mint") because it lets an unprivileged actor extract value from the system and impair the internal stablecoin's backing without any privileged action, governance abuse, or off-chain compromise.

### Likelihood Explanation
Likelihood is high in any deployment where the PSM approves external stablecoins that can realistically depeg (which is the entire point of a PSM — to interface with third-party stablecoins). No privileged role, oracle manipulation, governance action, or malicious peer/validator is required — a normal user with funds and a PSM's public `mint`/`redeem` extrinsics is sufficient. The only "cost" to the attacker is acquiring the depegged asset at its discounted market price, which is precisely the arbitrage the report's bug class enables.

### Recommendation
Introduce a price-aware conversion path for the PSM analogous to `pallet-asset-rate`'s `ConversionToAssetBalance`/`ConversionFromAssetBalance` traits [5](#0-4) , feeding a price oracle (or a governance-set/circuit-breaker-gated rate) into `external_to_internal`/`internal_to_external` conversions instead of assuming a fixed 1:1 par value. At minimum, add a per-external "depeg circuit breaker" that halts minting against an external asset once its observed/oracle price falls below a configured threshold, mirroring the pallet's existing "Circuit Breaker: Per-external emergency control to disable minting or all swaps" mechanism [6](#0-5)  but triggered by price rather than only by manual/administrative action.

### Proof of Concept
1. PSM instance for `INTERNAL_ASSET_ID` approves `USDC_ASSET_ID` as external asset via `add_external_asset`.
2. USDC depegs to $0.90 on the open market (e.g., due to a banking-partner issue), while the PSM has no oracle and continues to treat 1 USDC == 1 internal-unit (scaled only by decimals per `external_to_internal`) [7](#0-6) .
3. Attacker buys 1,000,000 USDC on the open market for $900,000.
4. Attacker calls `Psm::mint(origin, INTERNAL_ASSET_ID, USDC_ASSET_ID, 1_000_000 * UNIT, max_fee)`, receiving ~1,000,000 internal-asset tokens (minus `MintingFee`), which the protocol values at $1,000,000.
5. Attacker immediately profits ~$100,000 (minus minting fee) by holding/selling the internal asset at its assumed peg value, while the PSM's reserve is now backed by USDC that is worth less than the internal-asset liability recorded in `PsmDebt`, leaving the internal stablecoin under-collateralized.

### Citations

**File:** substrate/frame/psm/src/lib.rs (L18-21)
```rust
//! # Peg Stability Module (PSM) Pallet
//!
//! Instantiable Peg Stability Modules (PSMs). Each PSM enables 1:1 swaps between an internal
//! stablecoin and one or more approved external stablecoins, typically to maintain a peg.
```

**File:** substrate/frame/psm/src/lib.rs (L60-61)
```rust
//! * **PSM Debt**: Total internal asset minted through a PSM, backed 1:1 by external assets in that
//!   PSM's reserve.
```

**File:** substrate/frame/psm/src/lib.rs (L62-62)
```rust
//! * **Circuit Breaker**: Per-external emergency control to disable minting or all swaps.
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

**File:** substrate/frame/asset-rate/src/lib.rs (L246-253)
```rust
	fn from_asset_balance(
		balance: BalanceOf<T>,
		asset_kind: AssetKindOf<T>,
	) -> Result<BalanceOf<T>, pallet::Error<T>> {
		let rate = pallet::ConversionRateToNative::<T>::get(asset_kind)
			.ok_or(pallet::Error::<T>::UnknownAssetKind.into())?;
		Ok(rate.saturating_mul_int(balance))
	}
```

**File:** substrate/frame/support/src/traits/tokens/misc.rs (L296-313)
```rust
pub trait ConversionToAssetBalance<InBalance, AssetId, AssetBalance> {
	type Error;
	fn to_asset_balance(balance: InBalance, asset_id: AssetId)
		-> Result<AssetBalance, Self::Error>;
}

/// Converts an asset balance value into balance.
pub trait ConversionFromAssetBalance<AssetBalance, AssetId, OutBalance> {
	type Error;
	fn from_asset_balance(
		balance: AssetBalance,
		asset_id: AssetId,
	) -> Result<OutBalance, Self::Error>;
	/// Ensures that a conversion for the `asset_id` will be successful if done immediately after
	/// this call.
	#[cfg(feature = "runtime-benchmarks")]
	fn ensure_successful(asset_id: AssetId);
}
```
