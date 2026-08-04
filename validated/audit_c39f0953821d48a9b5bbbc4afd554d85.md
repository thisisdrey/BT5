## Title
Peg Stability Module assumes external stablecoins are always worth exactly par, causing over-redemption / undercollateralized reserve on depeg - (File: `substrate/frame/psm/src/lib.rs`)

## Summary
`pallet-psm` implements 1:1 swaps between an internal stablecoin and approved external stablecoins (e.g. USDC/USDT), converting purely by decimal scaling with no external price feed. `mint` and `redeem` treat `internal_to_external`/`external_to_internal` as a constant peg identical to the Oracle's `stablePrice` mechanism in the external report: once an external asset is registered, its market price is never revisited. If the external asset's real market value drops below par, `redeem` still pays out at the frozen 1:1 (decimals-adjusted) rate, letting any unprivileged signed account drain the PSM reserve of a higher-value asset (e.g. the internal stablecoin, or another external asset backing the debt) for a depegged one, or conversely mint internal stablecoin against a depegged external asset at full face value, without any oracle or peg-health check gating the operation.

## Finding Description
The pallet's core exchange functions never query a price oracle; they only compute decimal-scaled conversions: [1](#0-0) [2](#0-1) 

`mint` accepts external asset at face value and mints internal asset 1:1 (minus fee) purely via `external_to_internal`, gated only by `MintingStopped`/circuit breaker, min-swap size, and debt ceilings — none of which reference market price: [3](#0-2) 

`redeem` mirrors this: it burns internal asset and pays external asset via `internal_to_external`, gated only by `allows_redemption`, min-swap size, fee cap, and `InsufficientReserve` — again with no live price check: [4](#0-3) 

This is structurally identical to the reported Oracle bug: a value that should track live market conditions (`stablePrice` in the report, the implicit 1:1 external/internal rate here) is instead fixed and only ever changes through decimal-metadata snapshots (`ensure_decimals_match`), which govern unit scaling, not USD value. There is no `AssetPricer`-equivalent, no oracle hook, and no depeg circuit breaker keyed to price — the only circuit breaker (`ExternalAssetStatus`, `allows_minting`/`allows_redemption`) is a manually toggled admin flag, not an automatic price-based safeguard, per the doc comment: "**Circuit Breaker**: Per-external emergency control to disable minting or all swaps."

## Impact Explanation
If any approved external asset depegs (its market price falls below the internal stablecoin's peg), `redeem` still pays out external asset 1:1 against burned internal asset. Any signed, unprivileged account can call `redeem` to withdraw depegged collateral at face value from the PSM reserve while the debt ledger (`PsmDebt`) records the payout as if it were fully backed. Symmetrically, `mint` lets a caller deposit the depegging asset and receive internal stablecoin at full par value, directly minting stablecoin against under-collateralized backing. Because the PSM reserve can also hold multiple approved externals under one internal asset, or interconnect via `total_psm_debt`/`max_asset_debt`, a depeg in one external asset degrades backing for the shared internal-asset debt, risking insolvency of the whole PSM instance and loss of funds for anyone else holding or later redeeming the internal stablecoin — matching the report's "vault was undercollateralized at expiration" scenario. This is a direct value-conservation break (an unbacked/underpriced settlement) reachable by any ordinary user, not an admin or governance actor.

## Likelihood Explanation
High under real-world conditions: the pallet is explicitly designed to hold third-party stablecoins (USDC/USDT-style assets) whose peg is not guaranteed. The only mitigation is a manually-toggled `ExternalAssetStatus` circuit breaker that requires an admin/monitoring process to notice and react to a depeg — there is no automatic on-chain detection. Any window between a real-world depeg event and an admin disabling `redeem`/`mint` for that asset is directly and fully exploitable by ordinary users, exactly as described in the report's exploit scenario (USDC price drop while stale-peg logic still assumes par).

## Recommendation
- Short term: gate `mint`/`redeem` amounts against a live price oracle (or a bounded price-deviation check) for each `external_asset`, similar to setting an `AssetPricer` in the reported Oracle fix, so conversions reflect actual market value rather than an assumed constant peg; automatically restrict/disable an asset's `ExternalAssetStatus` when its observed price deviates beyond a configured band.
- Long term: make the PSM resilient to market conditions generally — support per-asset dynamic pricing, require redundant price sources, and add automated depeg detection that flips the circuit breaker without depending solely on manual admin intervention.

## Proof of Concept
1. Admin registers `internal_asset` (internal stablecoin) and approves `external_asset = USDC_ASSET_ID` via `add_external_asset`, decimals snapshotted at registration.
2. PSM accumulates internal-asset debt backed by USDC reserve through normal `mint` calls (external users depositing USDC, minting internal asset 1:1 minus fee), per `mint` at [5](#0-4) .
3. USDC depegs in the open market (drops to $0.80) — no on-chain price feed exists in this pallet, so nothing on-chain changes.
4. Before an admin manually disables `ExternalAssetStatus` for USDC, any signed attacker holding internal asset calls `redeem(internal_asset, USDC_ASSET_ID, internal_amount, max_fee)`:
   - `allows_redemption()` still returns true (no automatic price gate).
   - `internal_to_external` computes payout purely by decimal scaling, at full par, per [6](#0-5) .
   - Attacker receives USDC at face value even though its real value is $0.80/unit, extracting more value than the internal asset they burned was actually worth, while `PsmDebt` accounting treats the swap as fully par-backed.
5. Repeating this drains the PSM's USDC reserve at the expense of the internal stablecoin's actual backing, leaving remaining internal-asset holders under-collateralized — the same "USD 100 profit but only USD 80 backing" mismatch described in the source report, now realized as reserve drain rather than option mispricing.

### Citations

**File:** substrate/frame/psm/src/lib.rs (L18-21)
```rust
//! # Peg Stability Module (PSM) Pallet
//!
//! Instantiable Peg Stability Modules (PSMs). Each PSM enables 1:1 swaps between an internal
//! stablecoin and one or more approved external stablecoins, typically to maintain a peg.
```

**File:** substrate/frame/psm/src/lib.rs (L44-50)
```rust
//! A PSM strengthens its internal asset's peg by providing arbitrage opportunities:
//! - When the internal asset trades **above** $1: Users swap external assets for the internal asset
//!   and sell for profit.
//! - When the internal asset trades **below** $1: Users buy cheap internal asset and swap for
//!   external assets.
//!
//! This creates a price corridor bounded by the minting and redemption fees.
```

**File:** substrate/frame/psm/src/lib.rs (L702-751)
```rust
		pub fn mint(
			origin: OriginFor<T>,
			internal_asset: T::AssetId,
			external_asset: T::AssetId,
			external_amount: BalanceOf<T>,
			max_fee: Permill,
		) -> DispatchResult {
			let who = ensure_signed(origin)?;
			let info = Psm::<T>::get(&internal_asset).ok_or(Error::<T>::PsmNotFound)?;

			let external = ExternalAssets::<T>::get(&internal_asset, &external_asset)
				.ok_or(Error::<T>::UnsupportedAsset)?;
			ensure!(external.status.allows_minting(), Error::<T>::MintingStopped);

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
```

**File:** substrate/frame/psm/src/lib.rs (L811-850)
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

```
