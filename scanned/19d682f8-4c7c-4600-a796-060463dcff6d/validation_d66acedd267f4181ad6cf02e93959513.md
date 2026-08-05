## Analysis

The external report's core broken invariant: `PSM._redeem()` (Solidity) burns the internal token via a low-level primitive (`IERC20Burnable.burn`) that bypasses guard state (`paused()`/redeem-restriction) which is normally enforced by the *entry point* the guard was designed for (`yzUSD.redeem()`/`maxRedeem()`). The PSM's own checks (order-filler role, etc.) are a different, narrower set of checks that don't cover the underlying token's halt state.

This repository contains a directly analogous local module: `pallet-psm` (Peg Stability Module), which implements the same mint/redeem/circuit-breaker pattern in Substrate. [1](#0-0) 

### Title
PSM redeem bypasses internal-asset freeze because it checks only the per-external circuit breaker, not the internal asset's `AssetStatus` - (File: `substrate/frame/psm/src/lib.rs`)

### Summary
`Pallet::redeem` in `pallet-psm` only checks the per-`(internal_asset, external_asset)` circuit breaker (`external.status.allows_redemption()`) before calling `T::Fungibles::burn_from` on the internal asset. It never checks the internal asset's own `AssetStatus` (e.g. `Frozen`, set via `pallet-assets::freeze_asset`). Because `pallet-assets::do_burn` explicitly permits burning while an asset is `Frozen`, `redeem()` can still convert a user's frozen internal-stablecoin balance into external reserve assets during the exact emergency window a freeze is meant to halt.

### Finding Description
`Psm::redeem` gates redemption solely on the PSM-local circuit breaker stored in `ExternalAssets`: [2](#0-1) 

It then burns the internal asset directly via the low-level `Fungibles::burn_from` primitive: [3](#0-2) 

`pallet-assets::do_burn` (the implementation behind `burn_from`) deliberately allows burning while the asset's status is `Frozen`, not only `Live`: [4](#0-3) 

This is intentional generic behavior in `pallet-assets` (freezing halts ordinary transfers but not administrative/forced burns), but when composed with `pallet-psm`, it recreates precisely the bug class from the report: a public entry point (`redeem`, callable by any signed account) reaches a low-level state-mutating primitive (`burn_from`) that bypasses a guard (`AssetStatus::Frozen`) which exists specifically to halt movement/value-extraction of the asset. `PSM::redeem`'s own guard (`CircuitBreakerLevel`) is a different, narrower check scoped to the `(internal, external)` pair and says nothing about the internal asset's own frozen/halted state.

### Impact Explanation
If a governance/admin actor freezes the internal stablecoin asset (e.g., in response to a depeg, an exploit in another pallet that mints excess internal supply, or a compromised minter) with the expectation that all balance movement of that asset halts, `PSM::redeem` remains fully callable. Users can burn their frozen internal-asset balance and pull the PSM's external reserve (e.g. USDC) out at a 1:1 (minus fee) rate, converting an asset that was intentionally rendered non-transferable into liquid external funds. This drains the PSM reserve exactly during the window the freeze was meant to protect, causing fund loss/drain from the reserve to users who should have been blocked, and defeats the purpose of the freeze as an emergency control.

### Likelihood Explanation
Likelihood is Medium: it requires an admin to have frozen the internal asset (a normal incident-response action, not an attacker precondition) and a user to then call `redeem`, which is a permissionless, public dispatchable with no privileged actor needed on the attacker's side. No malicious validator/collator/relayer is required — any signed account holding the frozen internal asset can exploit it as soon as the freeze is set, which is exactly when the exploit is most valuable.

### Recommendation
In `Psm::redeem` (and `Psm::mint`, for the same reasoning on the external asset side), explicitly check the internal asset's own status (`Asset::<T,I>::get(internal_asset).status == AssetStatus::Live`, or equivalent via `T::Fungibles`) before burning, or ensure the PSM's circuit breaker is asserted to cover the internal asset's frozen state as well. Apply the check consistently for every entry point that leads into `burn_from`/`mint_into` on the internal asset.

### Proof of Concept
1. In `substrate/frame/psm/src/tests.rs` mock runtime, mint `ALICE` some internal asset via `Psm::mint`.
2. As root, freeze the internal asset: `pallet_assets::Pallet::<Test>::freeze_asset(RuntimeOrigin::root(), INTERNAL_ASSET_ID)` (setting `AssetStatus::Frozen`).
3. Call `Psm::redeem(RuntimeOrigin::signed(ALICE), INTERNAL_ASSET_ID, USDC_ASSET_ID, amount, Permill::zero())`.
4. Observe the call succeeds: `T::Fungibles::burn_from` on the frozen internal asset does not error (per `do_burn`'s `Live || Frozen` check), and ALICE receives external asset from the PSM reserve — demonstrating the freeze is bypassed by the PSM redemption path while `pallet_assets::transfer` of the same frozen asset would be rejected.

### Citations

**File:** substrate/frame/psm/src/lib.rs (L18-62)
```rust
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
```

**File:** substrate/frame/psm/src/lib.rs (L811-833)
```rust
		pub fn redeem(
			origin: OriginFor<T>,
			internal_asset: T::AssetId,
			external_asset: T::AssetId,
			internal_amount: BalanceOf<T>,
			max_fee: Permill,
		) -> DispatchResult {
			let who = ensure_signed(origin)?;
			let info = Psm::<T>::get(&internal_asset).ok_or(Error::<T>::PsmNotFound)?;

			let external = ExternalAssets::<T>::get(&internal_asset, &external_asset)
				.ok_or(Error::<T>::UnsupportedAsset)?;
			ensure!(external.status.allows_redemption(), Error::<T>::AllSwapsStopped);

			let ext_decimals = external.decimals;
			let internal_decimals = info.internal_decimals;

			ensure!(internal_amount >= info.min_swap_amount, Error::<T>::BelowMinimumSwap);

			let fee_rate = RedemptionFee::<T>::get(&internal_asset, &external_asset);
			ensure!(fee_rate <= max_fee, Error::<T>::FeeTooHigh);
			let fee = fee_rate.mul_ceil(internal_amount);
			let internal_net = internal_amount.saturating_sub(fee);
```

**File:** substrate/frame/psm/src/lib.rs (L867-887)
```rust
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

**File:** substrate/frame/assets/src/functions.rs (L534-546)
```rust
	pub(super) fn do_burn(
		id: T::AssetId,
		target: &T::AccountId,
		amount: T::Balance,
		maybe_check_admin: Option<T::AccountId>,
		f: DebitFlags,
	) -> Result<T::Balance, DispatchError> {
		let d = Asset::<T, I>::get(&id).ok_or(Error::<T, I>::Unknown)?;
		ensure!(
			d.status == AssetStatus::Live || d.status == AssetStatus::Frozen,
			Error::<T, I>::IncorrectStatus
		);

```
