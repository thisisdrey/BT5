## Analysis

This is my final answer based on the evidence gathered. The `pallet-psm` (Peg Stability Module) is the strongest local analog to the gETH oracle `_sanityCheck` bug, and I have enough evidence from `substrate/frame/psm/src/lib.rs` to support it. The other candidates I explored (nomination-pools `ok_to_be_open`/`MaxPointsToBalance`, and `pallet-staking-async` `OffenceQueue`/`UnappliedSlashes`) do not fit as well: the nomination-pools ratio is *always* computed live from current bonded balance (no staleness), and the staking-async offence queue explicitly blocks withdrawals until slashes are applied (`UnappliedSlashesInPreviousEra`), which is a defensive fix, not the bug pattern. The PSM pallet, by contrast, has no oracle/sanity-check on the *external* asset's real value at all — it assumes a permanent 1:1 peg — which is the same root cause as the report (protocol accepts/produces conversions at a rate that no longer reflects reality after value divergence).

### Title
Peg Stability Module assumes permanent 1:1 external-asset value with no devaluation check, enabling drain of reserve after depeg - (File: substrate/frame/psm/src/lib.rs)

### Summary
`pallet-psm`'s `mint`/`redeem` extrinsics convert between an "internal" stablecoin and an "external" asset using only a fixed decimals-based scaling (`external_to_internal`/`internal_to_external`), never checking whether the external asset is still actually worth 1:1 vs the internal asset. This mirrors the reported gETH bug: the protocol has no mechanism analogous to a price sanity check that reacts to a "slashing"-like devaluation event of the backing asset, so once the external asset devalues, the reserve can be drained at the stale 1:1 rate, converting the PSM's remaining debt-backing into worthless internal tokens.

### Finding Description
`Pallet::mint` at [1](#0-0)  transfers `external_amount` of the external asset into the PSM reserve and mints internal asset 1:1 (minus fee) purely via decimal-scaling conversion helpers, with no reference to any real market price or asset-health signal. `Pallet::redeem` at [2](#0-1)  burns internal asset and pays out external asset from the reserve at the same fixed 1:1 scaled rate, gated only by `InsufficientReserve` (whether the PSM literally holds enough units of the external asset) — not by whether that external asset retains its intended value.

The pallet's own doc explicitly states the peg mechanism relies on arbitrage keeping the *internal* asset price anchored to $1 [3](#0-2) , but there is no analogous mechanism, oracle check, or "sanity check" limiting mint/redeem when the *external* asset itself devalues (the equivalent of a slashing event in the gETH report). The only protective controls are debt ceilings (`max_debt`, `AssetCeilingWeight`) and a manually-triggered `CircuitBreakerLevel` [4](#0-3) , both of which require an admin/governance action to react — there is no automatic, permissionless mechanism that halts or re-prices swaps when the external asset's real value diverges from the assumed 1:1 peg.

This is the direct structural analog of the reported bug: just as `_sanityCheck` in the external report fails to account for slashing devaluing the backing ETH, causing gETH to be arbitraged to zero at a stale price, `pallet-psm` has no mechanism at all to detect or react to the external asset devaluing, so the internal asset can be minted/redeemed at a stale (assumed 1:1) rate against a devalued external asset, letting an attacker or arbitrageur convert real backing into overvalued internal claims before an admin manually intervenes with the circuit breaker.

### Impact Explanation
If the external asset (e.g., a wrapped/bridged asset) depegs or devalues after being deposited in the PSM reserve, any user can permissionlessly call `mint`/`redeem` at the un-adjusted 1:1 rate. This lets a user: (1) mint internal asset with newly-devalued external asset at face value, immediately diluting the peg backing for all existing internal-asset holders, or (2) if the external asset is still nominally in the reserve but has lost real value, redeem previously-deposited internal asset for real, still-valuable external asset before the reserve depletes — a race that favors whoever acts first (analogous to the reported "incentivized to withdraw as soon as possible" run). Because there is no automatic guard, this is a `theft or unbacked mint` / `permanent user-fund lock` class impact on whichever asset is favorably mispriced, potentially devaluing the internal asset toward zero exactly as described in the report for gETH.

### Likelihood Explanation
Likelihood depends entirely on runtime configuration: any external asset approved via `add_external_asset` that is not a rock-solid, unconditionally-pegged asset (e.g., a bridged/wrapped token, an LST, or any asset subject to slashing/de-pegging) exposes this gap. Since `pallet-psm` is a generic, reusable pallet intended to be parameterized by runtime integrators with arbitrary `AssetId`s, and nothing in the pallet itself prevents approving a volatile or slashable asset as "external," the precondition (an external asset that can devalue) is realistic and outside attacker control — it requires no malicious validator, governance abuse, or privileged actor, only normal usage of the pallet with a real-world asset that loses value.

### Recommendation
Do not treat 1:1 peg as a permanent invariant. Integrate a price/health oracle (or a bounded automatic circuit-breaker keyed on external asset devaluation, analogous to the reported recommendation for gETH) so that `mint`/`redeem` conversions are suspended or re-priced automatically once the external asset's observed value diverges materially from parity, rather than relying solely on manual `set_circuit_breaker` intervention by `emergency_admin`.

### Proof of Concept
1. Runtime configures a PSM with `internal_asset` = protocol stablecoin and `external_asset` = a bridged/liquid-staked token `X` currently trading at parity.
2. Users mint internal asset by depositing `X` into the PSM reserve via `Pallet::mint` [5](#0-4) , building up reserve and `PsmDebt`.
3. `X` suffers a devaluation event (e.g., underlying slashing on its chain, or a bridge-side accounting loss) so its real market value drops sharply, but the PSM has no oracle and continues to treat 1 unit of `X` as worth 1 unit of internal asset (scaled only by decimals) via `external_to_internal`/`internal_to_external`.
4. Before `emergency_admin` notices and manually sets `CircuitBreakerLevel::AllDisabled`, an attacker calls `Pallet::redeem` [6](#0-5)  to swap internal asset (bought cheaply elsewhere, since the market already re-priced it down) back for the still-nominally-valued `X` reserve, or mints new internal asset against freshly devalued `X`, extracting value from the reserve/protocol at the stale 1:1 rate — the same "arbitrage opportunity" dynamic described in the original report, gated only by `InsufficientReserve`/`max_debt`, neither of which reacts to price.

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

**File:** substrate/frame/psm/src/lib.rs (L700-767)
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

			Self::deposit_event(Event::Minted {
				who,
				internal_asset,
				external_asset,
				external_consumed: effective_external,
				internal_received: internal_to_user,
				internal_fee: fee,
			});
			Ok(())
		}
```

**File:** substrate/frame/psm/src/lib.rs (L809-902)
```rust
		#[pallet::call_index(1)]
		#[pallet::weight(T::WeightInfo::redeem())]
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

			PsmDebt::<T>::mutate(&internal_asset, &external_asset, |debt| {
				*debt = debt.saturating_sub(effective_internal_net);
			});

			Self::deposit_event(Event::Redeemed {
				who,
				internal_asset,
				external_asset,
				internal_consumed: effective_internal_net.saturating_add(fee),
				external_received: external_out,
				internal_fee: fee,
			});
			Ok(())
		}
```
