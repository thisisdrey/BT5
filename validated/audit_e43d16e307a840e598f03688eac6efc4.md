### Title
`pallet-psm` mint/redeem enforce a hard-coded 1:1 exchange rate with no external price check, enabling unlimited-arbitrage drain of PSM reserves on stablecoin depeg - (File: `substrate/frame/psm/src/lib.rs`)

### Summary
`pallet-psm` is a direct local analog of the `CCFrax1to1AMM` finding: it is a constant-sum, zero-slippage swap mechanism that assumes external stablecoins are always worth exactly 1 unit of the internal stablecoin (adjusted only for decimals), with no oracle or market-price input. The only bounds on exposure are governance-set, static values (`max_debt`, `AssetCeilingWeight`), which — exactly as in the Trail-of-Bits report — do not scale with how far the external asset's real market price has diverged from peg. Once an approved external stablecoin depegs downward, an unprivileged caller can mint internal asset 1:1 against devalued collateral up to the ceiling, extracting value from the reserve and leaving later redeemers to absorb the loss.

### Finding Description
`mint` [1](#0-0)  converts `external_amount` to `internal_equivalent` purely via `external_to_internal`, a decimals-only scaling helper — there is no price oracle or market-rate input anywhere in the pallet (confirmed by the absence of any `oracle`/`market_price` reference in the file). The pallet's own documentation states the peg-stability design explicitly assumes external assets trade at $1 [2](#0-1) .

The only guards against abuse are:
- `min_swap_amount` / `BelowMinimumSwap` (a floor, not a price check),
- `max_debt` (a static, governance-set aggregate debt ceiling) checked in `mint` [3](#0-2) ,
- `AssetCeilingWeight` (a static per-asset weight, also governance-set),
- fees of ~0.5% (`DefaultFee`) [4](#0-3) ,
- circuit breakers that must be manually flipped by an emergency admin [5](#0-4) .

None of these bounds are a function of the *actual* price divergence between the external asset and $1, exactly mirroring the audited flaw: "While `token_price` swings are limited by the `price_tolerance` parameter, `frax_price` swings are not limited" — here, there is no price parameter at all, only a static notional ceiling. As with `CCFrax1to1AMM`, once the external asset's market price drops meaningfully below peg, the most profitable strategy is to buy the depegged asset cheaply off-chain and swap it into the PSM at the fixed 1:1 (minus 0.5% fee) rate via the public, unprivileged `mint` extrinsic — up to `max_debt`/ceiling weight — realizing near risk-free arbitrage profit funded by whichever previously-deposited, still-solvent collateral remains in the PSM reserve for other holders to redeem against.

### Impact Explanation
This directly matches the "theft or unbacked mint" and "public underpriced work" impact categories: internal stablecoin is minted against collateral objectively worth less than face value, silently under-collateralizing the PSM. Later redeemers calling `redeem` [6](#0-5)  draw down the reserve at face value too, so whichever external asset in the reserve retains full value gets drained first, socializing the loss from the depegged asset onto remaining internal-asset holders and the runtime that backs the peg. The exposure is bounded only by the static `max_debt`/ceiling (an admin-chosen constant), not by any measure of real economic risk — for a PSM instance sized to be useful (e.g., millions of internal-asset units), this is a large, unbacked-mint / fund-loss vector requiring no privileged actor.

### Likelihood Explanation
Likelihood is realistic and does not require any malicious peer, validator, collator, or admin: any signed account can call `mint`/`redeem` today. The precondition is solely that an approved external stablecoin depegs in the open market — a routine, historically observed event for many stablecoins — which is an external-market condition, not an on-chain admin failure. No governance, relayer, or prover assumption is needed; the vulnerable path is the pallet's normal, intended dispatch flow.

### Recommendation
- Do not treat approved external assets as unconditionally worth exactly 1:1; integrate a price oracle or bounded price-deviation check (analogous to `price_tolerance` in the report) before allowing `mint`/`redeem` to execute at par.
- Make the effective debt ceiling and/or fee dynamically responsive to observed price deviation (e.g., automatically tightening or halting minting for an asset whose oracle price departs from peg beyond a threshold), rather than relying solely on a static, governance-set `max_debt`/`AssetCeilingWeight`.
- Consider requiring `emergency_admin` circuit-breaker responsiveness to be automatable/triggerable off an oracle feed rather than purely manual, since manual intervention is too slow to prevent arbitrage during a rapid depeg.

### Proof of Concept
1. Governance creates a PSM for internal asset `pUSD` and approves external asset `USDX` with `max_debt = 10_000_000` and default 0.5% fees, via `create_psm`/`add_external_asset` (both governance-only, not attacker-controlled).
2. `USDX` depegs on external markets to $0.90 due to a market event unrelated to this chain.
3. Attacker buys `10,000,000` `USDX` on the open market for `$9,000,000`.
4. Attacker calls `Psm::mint(origin, pUSD, USDX, 10_000_000, max_fee)` [7](#0-6) , receiving `~9,950,000 pUSD` (10,000,000 minus 0.5% fee), since the pallet performs only decimal-scaled 1:1 conversion with no market-price check.
5. Attacker sells the `pUSD` on the open market (or via any DEX/PSM-external redemption path) for ~$1 each, realizing ~`$950,000` profit while the PSM reserve now holds `10,000,000 USDX` actually worth `$9,000,000` backing `~9,950,000 pUSD` face value outstanding.
6. Subsequent legitimate `redeem` calls [8](#0-7)  pay out `USDX` 1:1 in face value terms, meaning the last redeemers absorb the shortfall — the reserve is left permanently under-collateralized relative to outstanding `pUSD` debt.

Note: I could not execute this scenario against a live/test environment (index access only); the analysis is based on the pallet source, its inline documentation, and its test/mock scaffolding referenced in the repository, and does not account for any oracle-based safeguard that might exist in a downstream runtime configuration not present in this pallet's code.

### Citations

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

**File:** substrate/frame/psm/src/lib.rs (L167-187)
```rust
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

**File:** substrate/frame/psm/src/lib.rs (L262-268)
```rust
	/// Suggested fee of 0.5% for minting and redemption.
	pub(crate) struct DefaultFee;
	impl Get<Permill> for DefaultFee {
		fn get() -> Permill {
			Permill::from_parts(5_000)
		}
	}
```

**File:** substrate/frame/psm/src/lib.rs (L700-725)
```rust
		#[pallet::call_index(0)]
		#[pallet::weight(T::WeightInfo::mint(T::MaxExternals::get()))]
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
```

**File:** substrate/frame/psm/src/lib.rs (L732-741)
```rust
			let current_total_psm_debt = Self::total_psm_debt(&internal_asset);
			ensure!(
				current_total_psm_debt.saturating_add(internal_equivalent) <= info.max_debt,
				Error::<T>::ExceedsMaxPsmDebt
			);

			let current_debt = PsmDebt::<T>::get(&internal_asset, &external_asset);
			let max_debt = Self::max_asset_debt(&internal_asset, &external_asset, &info);
			let new_debt = current_debt.saturating_add(internal_equivalent);
			ensure!(new_debt <= max_debt, Error::<T>::ExceedsMaxPsmDebt);
```

**File:** substrate/frame/psm/src/lib.rs (L811-887)
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
