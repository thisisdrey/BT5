Audit Report

## Title
`pallet-psm::mint`/`redeem` convert external stablecoins to internal stablecoin at a hardcoded 1:1 (decimals-adjusted) rate with no price or depeg check, enabling unbacked minting and reserve drain when an approved external asset depegs - (File: `substrate/frame/psm/src/lib.rs`)

## Summary
`pallet-psm::mint` and `redeem` convert between an "internal" stablecoin and any approved "external" asset using only decimal-scaling arithmetic (`external_to_internal`/`internal_to_external`), with no oracle, price feed, or peg-deviation check anywhere in the swap path. Because `get_reserve`/`psm_account` derive a single shared reserve account per internal asset, and multiple external assets can be approved on the same instance, all reserves are fungible across externals via the shared internal asset, letting a depegged external be minted 1:1 and then redeemed against a correctly pegged external.

## Finding Description
`mint` at [1](#0-0)  computes `internal_equivalent` purely via `Self::external_to_internal`, a decimal-scaling-only function shown at [2](#0-1) . The only checks performed are decimal-snapshot consistency (`ensure_decimals_match`), minimum swap size, minting-fee cap, and debt-ceiling checks (`ExceedsMaxPsmDebt`) — no price or exchange-rate validation exists. `redeem`, at [3](#0-2) , mirrors this: it computes `external_out` via `internal_to_external` and pays it from `Self::get_reserve`, which reads the balance of `external_asset` held by the shared per-internal-asset reserve account, `Self::psm_account(internal_asset)` [4](#0-3) . Since `ExternalAssets`, `AssetCeilingWeight`, and `PsmDebt` are all keyed per `(internal_asset, external_asset)` but the total debt ceiling (`total_psm_debt`, `max_debt`) is enforced in aggregate across all approved externals of the same instance [5](#0-4) , an attacker can mint against a depegged external `A` (receiving internal asset at full nominal value) and redeem against a correctly-pegged external `B`, extracting `B`'s reserve balance held in the shared PSM account, as confirmed by `get_reserve`'s implementation reading balance keyed only by `external_asset` at the shared `psm_account`.

## Impact Explanation
This is an unbacked-value-extraction / theft-of-reserve issue matching the "theft or unbacked mint" impact category: an unprivileged signed account can convert a depegged external asset into full nominal-value internal asset, then drain another approved external asset's reserve backing other holders, with no protocol-level guard preventing it. The only existing safeguard, the per-external circuit breaker (`CircuitBreakerLevel`/`ExternalAssetInfo::status`), is a manual, admin-triggered control that only stops the attack after the fact, leaving an exploitable window between depeg and admin reaction — this is explicitly acknowledged in code comments but not compensated for on-chain.

## Likelihood Explanation
The attack requires only (1) a PSM instance configured with two or more approved external assets sharing a common internal asset and reserve account, and (2) one of those externals depegging on the open market — a condition entirely outside chain control and not requiring any validator, node, or governance compromise. Given `MaxExternals`, `ExternalAssets`, and `AssetCeilingWeight` are designed specifically to support multiple externals per instance, this configuration is a normal, expected deployment pattern, making the precondition realistic. Once a depeg occurs, exploitation requires only two ordinary signed extrinsic calls (`mint` then `redeem`) and is fully repeatable until an admin manually intervenes via the circuit breaker.

## Recommendation
Add an oracle-based or bounded peg-deviation check before allowing `mint`/`redeem` to proceed at par value, or eliminate cross-asset fungibility of the shared reserve by tracking and settling redemptions against the specific external asset's own contributed reserve rather than the pooled account balance, so a depegged external cannot be exchanged for a correctly pegged one held in the same instance's reserve. Automatic circuit-breaking on detected price deviation (rather than manual-only admin action) would close the exploit window.

## Proof of Concept
1. Configure a PSM instance for `internal_asset` with two approved externals `A` and `B`, both decimal-matched via `add_external_asset`/`ensure_decimals_match`, sharing one `psm_account(internal_asset)` reserve.
2. `A` depegs to $0.50 on the open market. Attacker buys 1000 units of `A` for $500.
3. Attacker calls `mint(internal_asset, A, 1000, max_fee)` per [1](#0-0) , receiving ~1000 units of `internal_asset` (minus fee) with no price check.
4. Attacker calls `redeem(internal_asset, B, 1000, max_fee)` per [3](#0-2) , receiving ~1000 units of `B` (worth $1000) drawn from `get_reserve` at the shared `psm_account`.
5. Net result: attacker converts $500 of depegged `A` into $1000 of `B`, draining reserve backing `B` holders with no on-chain guard preventing it. A Rust integration test in `substrate/frame/psm/src/tests.rs` mocking a depeg (simply by not adjusting price, since no price input exists at all) confirms `mint`/`redeem` succeed unconditionally as long as decimal, fee-cap, minimum-swap, and debt-ceiling checks pass.

### Citations

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

**File:** substrate/frame/psm/src/lib.rs (L811-901)
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
```

**File:** substrate/frame/psm/src/lib.rs (L1552-1556)
```rust
		/// Total internal-asset debt minted through a PSM instance.
		pub(crate) fn total_psm_debt(internal_asset: &T::AssetId) -> BalanceOf<T> {
			PsmDebt::<T>::iter_prefix_values(internal_asset)
				.fold(BalanceOf::<T>::zero(), |acc, debt| acc.saturating_add(debt))
		}
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
