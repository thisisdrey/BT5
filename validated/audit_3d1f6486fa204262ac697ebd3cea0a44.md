## Finding

The `pallet-psm` (Peg Stability Module) pallet contains the same "incorrect price assumption" bug class as the Umee `GetExchangeRateBase` report: it treats every approved external asset as worth exactly the same as the internal stablecoin (a hardcoded 1:1 parity, adjusted only for decimal scaling) with **no live price check at all**, in the public `mint`/`redeem` extrinsics.

### Title
Unconditional 1:1 price assumption in `pallet_psm::mint`/`redeem` allows draining PSM reserves when an approved external stablecoin depegs - (File: `substrate/frame/psm/src/lib.rs`)

### Summary
`pallet-psm` swaps an "internal" stablecoin for an "external" one (e.g. USDC/USDT) using only decimal-scaling conversion (`external_to_internal` / `internal_to_external`), never consulting a price oracle. The pallet's own docs affirm the 1:1 assumption: "A PSM strengthens its internal asset's peg... When the internal asset trades below $1: Users buy cheap internal asset and swap for external assets" — but there is no check that the *external* asset itself is still worth $1. [1](#0-0) [2](#0-1) 

### Finding Description
`mint` (call_index 0) computes `internal_equivalent` purely via `external_to_internal(external_amount, ext_decimals, internal_decimals)`, which only rescales for decimal precision, then mints that same value 1:1 (minus fee) of the internal stablecoin against the deposited external asset: [3](#0-2) 

`redeem` (call_index 1) does the symmetric 1:1 conversion back: [4](#0-3) 

Neither path references any oracle, `pallet-asset-rate`, or `pallet-asset-conversion` pool price — it is exactly the Umee pattern of assuming any name-listed "stablecoin" is worth $1 regardless of its real market value. This mirrors "Exploit Scenario 2" in the source report verbatim: *"The price of a stablecoin drops significantly... the module fails to detect the change and reports the price as USD 1."*

Existing guards (`max_debt`, per-asset ceiling weight, circuit breaker, decimals-match check via `ensure_decimals_match`) only bound *volume* and *decimal-precision correctness* — none of them validate that 1 unit of the external asset is actually worth 1 unit of internal asset at the time of the swap. Once an external asset is registered via `add_external_asset` (a normal, one-time admin action, not itself malicious), any unprivileged signed user can call `mint`/`redeem` freely thereafter, for the life of the listing.

### Impact Explanation
If any approved external asset later depegs (a routine market event, not an admin/insider compromise), an attacker can:
1. Buy the depegged external asset cheaply on the open market (e.g. at $0.50).
2. Call `mint` to deposit it into the PSM and receive the internal stablecoin at full 1:1 par value.
3. Immediately sell/redeem the newly minted internal stablecoin elsewhere at its intact peg, realizing the price gap as profit while leaving the PSM reserve permanently under-collateralized by the depegged asset.

This is unbacked mint / theft of reserve value with no reliance on a malicious peer, validator, relayer, or governance actor — the trigger is solely an external market price move plus a normal public extrinsic call, which is in scope per the impact gate ("theft or unbacked mint or unlock").

### Likelihood Explanation
Any PSM instance that lists a real-world stablecoin (the pallet's stated primary use case, per its own docs and `README.md`) is exposed the moment that stablecoin's peg weakens even temporarily — a common, recurring event for stablecoins historically (USDC's 2023 depeg to ~$0.87, UST's collapse, etc.). No special privileges, timing races, or governance compromise are needed; a single signed account with capital can execute the mint/redeem sequence as soon as a depeg is observed.

### Recommendation
Do not assume unconditional 1:1 parity in `mint`/`redeem`. Either:
- Integrate a price-feed/oracle check (e.g. via `pallet-asset-rate` or a configurable `PriceOracle` trait) and reject or throttle swaps when the external asset's live price deviates from its pegged value beyond a configurable tolerance, or
- Add an emergency circuit-breaker trigger tied to an oracle/price-deviation signal (in addition to the existing manually-set `CircuitBreakerLevel`) so depegs can be detected and minting halted automatically rather than relying on manual admin intervention after losses have already occurred.

### Proof of Concept
1. Admin creates a PSM with internal asset `pUSD` and registers external asset `USDC` via `create_psm` + `add_external_asset` + `set_asset_ceiling_weight` (normal setup, not malicious).
2. USDC depegs on secondary markets to $0.50 (external market event).
3. Attacker buys 1,000,000 USDC for $500,000 on the open market.
4. Attacker calls `Psm::mint(origin, pUSD, USDC, 1_000_000, max_fee)` — per [5](#0-4) , this mints ~1,000,000 `pUSD` (minus fee) regardless of USDC's real value.
5. Attacker sells the 1,000,000 `pUSD` (which the market still treats as ~$1 par) for ~$1,000,000, netting ~$500,000 profit while the PSM reserve now holds only the depegged, devalued USDC as backing for the outstanding `pUSD` debt.

### Citations

**File:** substrate/frame/psm/src/lib.rs (L18-21)
```rust
//! # Peg Stability Module (PSM) Pallet
//!
//! Instantiable Peg Stability Modules (PSMs). Each PSM enables 1:1 swaps between an internal
//! stablecoin and one or more approved external stablecoins, typically to maintain a peg.
```

**File:** substrate/frame/psm/src/lib.rs (L716-756)
```rust
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
```

**File:** substrate/frame/psm/src/lib.rs (L825-846)
```rust
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
```

**File:** substrate/frame/psm/src/lib.rs (L1605-1624)
```rust
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
