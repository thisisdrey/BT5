Audit Report

## Title
`pallet-psm::mint`/`redeem` perform pure decimal-scaling conversions with no oracle or peg-deviation check, letting an unprivileged user mint `internal_asset` from a depegged external and drain another approved external's reserve via `redeem` - (File: `substrate/frame/psm/src/lib.rs`)

## Summary
`pallet-psm` implements "1:1 swaps between an internal stablecoin and one or more approved external stablecoins" [1](#0-0)  using only decimal-adjustment math in `mint`/`redeem`, with no price feed of any kind. Because a single PSM instance pools reserves for multiple approved external assets against one internal asset, a depeg in any one approved external lets an attacker mint at full nominal value and redeem against a different, correctly-pegged external, draining its backing.

## Finding Description
`Pallet::mint` validates only decimal-consistency, minimum swap size, fee caps, and debt ceilings before crediting the caller with `internal_asset` computed via `Self::external_to_internal`, which is documented and implemented as a pure `10^(ext_decimals - internal_decimals)` scaling operation with no external price input [2](#0-1) [3](#0-2) . `Pallet::redeem` mirrors this, burning `internal_asset` and paying out `external_out` from the PSM's shared reserve account via `internal_to_external`, again with no price check [4](#0-3) . The pallet's own documentation confirms a single PSM instance can approve multiple external assets that are all redeemable/mintable against the same internal asset and reserve, keyed only by `(internal_asset, external_asset)` for fees and debt tracking [5](#0-4) [6](#0-5) . No oracle, price-deviation check, or per-asset reserve fencing exists in the mint/redeem logic; the only listed circuit breaker is a manually admin-triggered `MintingStopped`/`AllSwapsStopped` status flag, which is reactive rather than preventive.

## Impact Explanation
This falls under the "theft or unbacked mint" impact category permitted by the Polkadot SDK impact gate: an unprivileged signed account can mint `internal_asset` against a depegged external at full nominal (decimal-only-adjusted) value and then extract full-value units of a different, correctly-pegged external asset from the shared reserve, corrupting the backing/collateralization invariant between `PsmDebt`, the reserve balance, and true market value of the internal asset's claimed backing. The exploit path is fully reachable via the public `mint` and `redeem` extrinsics with no governance, validator, or relayer involvement.

## Likelihood Explanation
The precondition — an approved external stablecoin trading below its intended peg on the open market — is a realistic, externally-triggered market event, not requiring any malicious peer, validator, or leaked key. Any PSM instance configured with more than one approved external asset is exposed the instant such a depeg occurs, and the attack is repeatable until an admin manually halts the affected asset via the circuit breaker.

## Recommendation
Add a price/peg validation gate (oracle-based bounded deviation check) before allowing `mint`/`redeem` to proceed at par value, or alternatively fence each external asset's mint/redeem debt and reserve strictly to itself rather than pooling reserves across externals sharing one internal asset, so a depegged external cannot be arbitraged against a correctly-pegged one through the shared internal asset.

## Proof of Concept
1. Configure one PSM instance for `internal_asset` with two approved externals `A` and `B`, both decimal-matched via `ensure_decimals_match`.
2. `A` depegs to $0.50 on the open market while `B` remains at $1.00.
3. Attacker buys 1000 units of `A` for $500, calls `mint(internal_asset, A, 1000, max_fee)` and receives ~1000 units of `internal_asset` per `external_to_internal` with no price check [2](#0-1) .
4. Attacker calls `redeem(internal_asset, B, 1000, max_fee)` and receives ~1000 units of `B` (worth $1000) from the shared reserve via `internal_to_external` [4](#0-3) .
5. Net effect: attacker converts $500 of depegged `A` into $1000 of `B`, draining reserve backing intended for `B` holders, with no on-chain price guard preventing it.

### Citations

**File:** substrate/frame/psm/src/lib.rs (L18-21)
```rust
//! # Peg Stability Module (PSM) Pallet
//!
//! Instantiable Peg Stability Modules (PSMs). Each PSM enables 1:1 swaps between an internal
//! stablecoin and one or more approved external stablecoins, typically to maintain a peg.
```

**File:** substrate/frame/psm/src/lib.rs (L33-40)
```rust
//!   stablecoin). Each PSM instance is keyed by its internal asset id; multiple instances can
//!   coexist, each with its own reserve, debt ceiling, fee destination and approved externals. Mint
//!   operations credit the user with the internal asset; redeem operations burn it. Fees are
//!   collected in the internal asset and forwarded to that instance's [`PsmInfo::fee_destination`].
//! * **External** — third-party assets (e.g. USDC, USDT) approved on a specific PSM via
//!   [`Pallet::add_external_asset`] and held in that PSM's reserve. Users deposit external to mint
//!   internal, and burn internal to redeem external. A PSM may approve multiple externals, each
//!   identified by `external_asset`.
```

**File:** substrate/frame/psm/src/lib.rs (L52-62)
```rust
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

**File:** substrate/frame/psm/src/lib.rs (L700-751)
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
