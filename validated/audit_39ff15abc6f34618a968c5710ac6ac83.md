## Analog Vulnerability Identified: `pallet-psm` mint/redeem assumes 1:1 peg with no price or depeg validation

### Title
`pallet-psm::mint`/`redeem` convert external stablecoins to internal stablecoin at a hardcoded 1:1 (decimals-adjusted) rate with no price or depeg check, enabling unbacked minting and reserve drain when an approved external asset depegs - (File: `substrate/frame/psm/src/lib.rs`)

### Summary
`pallet-psm` is a peg-stability-module analog to the reported `Minter` contract: it accepts a `baseAsset`-like "external asset" and mints an "internal asset" (a stablecoin, e.g. pUSD) 1:1 after only adjusting for decimal precision. The Syntetika fix cited in the report added exactly this kind of decimal-matching check, but explicitly left exchange-rate/depeg risk to an off-chain monitoring bot pausing the contract. `pallet-psm` has the same structural gap: `mint` and `redeem` perform `external_to_internal`/`internal_to_external` conversions that are purely decimal-scaling functions with no oracle or price input, so if any approved external asset trades below its intended $1 peg, an unprivileged user can mint the internal stablecoin at full nominal value and drain the PSM's reserves of other, correctly-pegged approved externals via `redeem`.

### Finding Description
`Pallet::mint` at [1](#0-0)  converts `external_amount` into `internal_equivalent` using only `Self::external_to_internal`, which is a pure decimal-scaling function: [2](#0-1) 

There is no exchange-rate, oracle, or depeg check anywhere in `mint` or `redeem` - the only validations are decimal-snapshot consistency (`ensure_decimals_match`), minimum swap size, fee caps, and debt ceilings: [3](#0-2) 

`redeem` mirrors this and pays out `external_out` from the shared PSM reserve account computed purely via `internal_to_external`: [4](#0-3) 

Because multiple external assets can be approved against the same `internal_asset` on one PSM instance (see `ExternalAssets`, `AssetCeilingWeight`, `max_asset_debt`), all external reserves for that instance are pooled and interchangeable via the internal asset. If external asset `A` (e.g. a stablecoin) depegs on the open market to below $1, any unprivileged user can:
1. Buy the depegged `A` cheaply on a DEX.
2. Call `mint` with `A`, receiving `internal_asset` at full par value (only decimal-adjusted, not price-adjusted).
3. Call `redeem` against a different, correctly-pegged external asset `B` approved on the same PSM instance, extracting full-value `B` from the shared reserve in exchange for the discounted internal asset.

This is functionally identical to the reported `Minter::mint`/`redeem` bug: deposit a depegged base asset, receive full nominal value of a synthetic/internal token, then drain reserves/pools backed by other, correctly valued assets.

### Impact Explanation
This directly matches the "theft or unbacked mint" and "public underpriced work" impact categories: unauthorized value extraction, unbacked minting of `internal_asset`, and draining of the PSM's pooled reserve accounted for other approved externals - all reachable by an ordinary signed account with no admin, governance, relayer, or validator involvement. The only mitigation mentioned by the vendor for the *original* report (an off-chain bot pausing on depeg) is absent from this pallet's on-chain logic entirely; the circuit breaker (`MintingStopped`/`AllSwapsStopped`) exists but requires manual admin intervention after the fact, so it does not prevent the attack window between a depeg occurring and an admin reacting.

### Likelihood Explanation
Likelihood is tied to real-world depeg events of any approved external asset, which are external market conditions (not requiring a malicious peer/validator/admin) - the same precondition accepted as realistic in the original report. Any PSM instance configured with more than one approved external asset, or where the single external asset's redemption proceeds have market value, is exposed the moment a depeg occurs, since nothing in `mint`/`redeem` reads a price feed.

### Recommendation
Introduce a price or peg-validation mechanism (e.g., Chainlink-style oracle, or a bounded acceptable price ratio) before allowing `mint`/`redeem`, or restrict `Self::external_to_internal`/`internal_to_external` usage such that debt/redemption per external asset is fenced to that asset's own reserve rather than a shared pool, so a depegged external cannot be exchanged for a correctly-pegged one. At minimum, add an automated peg-check gate that halts minting/redemption for an external asset when its live market price deviates beyond a threshold, rather than relying solely on manual admin-triggered circuit breakers.

### Proof of Concept
1. Configure a PSM instance for `internal_asset` (pUSD) with two approved externals: `A` (stablecoin, currently trading at $0.50 due to depeg) and `B` (stablecoin, trading at $1.00), both normal via `add_external_asset` and decimal-matched per `ensure_decimals_match`.
2. Attacker buys 1000 units of `A` on the open market for $500.
3. Attacker calls `Pallet::mint(internal_asset, A, 1000, max_fee)` → receives ~1000 units of `internal_asset` (minus fee), per [5](#0-4) , with no price check performed.
4. Attacker calls `Pallet::redeem(internal_asset, B, 1000, max_fee)` → receives ~1000 units of `B` (worth $1000) from the shared PSM reserve, per [6](#0-5) .
5. Net result: attacker converted $500 of depegged `A` into $1000 of `B`, draining the reserve backing `B` holders, with no on-chain guard preventing it.

### Citations

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
