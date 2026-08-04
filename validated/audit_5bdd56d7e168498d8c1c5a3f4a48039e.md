### Title
PSM pallet mints/redeems every approved stablecoin at a hard-coded 1:1 par value with no price oracle, letting arbitrageurs drain healthy collateral and leave the reserve stuck with a depegged asset - (File: `substrate/frame/psm/src/lib.rs`)

### Summary
`pallet-psm` implements a Peg Stability Module that mints/burns an internal stablecoin against any number of approved external stablecoins (e.g. USDC, USDT) at an unconditional 1:1 ratio, adjusted only for decimals, never for market price. This is the same broken invariant as the LevelMinting report: any registered collateral asset is treated as always worth exactly $1, and mint+redeem can be freely combined by any signed account with no cooldown, price check, or oracle. If one external asset depegs downward, an attacker can mint internal asset cheaply against the depegged collateral and immediately redeem it against a healthier external asset that already has outstanding debt in the same PSM instance, extracting the price difference risk-free and leaving the PSM's reserve backed by the worthless asset.

### Finding Description
`mint` deposits `external_amount` of `external_asset` and credits the caller with the decimal-normalized equivalent of the internal asset, with no reference to real-world price: [1](#0-0) 

`redeem` burns internal asset and pays out `external_asset` from the shared PSM reserve account, again purely via decimal conversion — no price check, no cooldown, and callable in the same transaction as a prior `mint`: [2](#0-1) 

The only constraints on redeeming a specific external asset are (a) that external's tracked `PsmDebt` and (b) the PSM reserve actually holding enough of it: [3](#0-2) 

A PSM instance can register multiple external assets (e.g. USDC and USDT) sharing one internal asset and one physical reserve account (`Self::psm_account(&internal_asset)`), as documented in the pallet's own overview: [4](#0-3) [5](#0-4) 

There is no price oracle integration anywhere in the pallet (confirmed by the absence of any `oracle`/`price` reference in `substrate/frame/psm/`), unlike LevelMinting's fix, which discounts mint/redeem amounts using Chainlink price feeds. Fees are admin-configured per asset pair and can be as low as 0% (the shipped Asset Hub Westend configuration sets USDT's minting fee to 0%): [6](#0-5) 

### Impact Explanation
If any approved external asset in a multi-collateral PSM instance depegs below $1 (e.g. a USDT-style depeg event), an unprivileged, signed attacker can:
1. `mint` internal asset by depositing the depegged asset at par (paying only the low/zero minting fee), increasing `PsmDebt[internal, depegged_asset]`.
2. `redeem` the same internal asset for a *different*, still-healthy external asset that has outstanding `PsmDebt` in the same instance (funded by earlier honest depositors), draining `psm_account` of the good asset at par.

This repeatedly transfers value from the shared reserve to the attacker with no fee-based cost beyond the configured (potentially 0%) mint/redeem fees, and progressively concentrates the PSM's reserve into the worthless depegged asset — mirroring the exact impact described in the LevelMinting report: the PSM ends up holding a stablecoin of lower value while honest holders of the internal asset are left backed by devalued collateral. Because `create_psm`/`add_external_asset` are the only privileged actions here, and mint/redeem themselves are fully public extrinsics, this is a live, unprivileged public-entrypoint fund-drain path, not an admin-abuse scenario.

### Likelihood Explanation
Likelihood is contingent on a genuine market depeg of one of the PSM's approved external stablecoins, which is an external market event, not an attacker precondition — the attacker only needs standard signed-extrinsic access once such a depeg occurs. The pallet provides no automatic circuit breaker tied to price; only a manual admin action (`set_asset_status`) can halt minting/redemption for the depegged asset, and this requires the `full_admin`/`emergency_admin` to react before/during the depeg window. Given real depeg events happen with limited warning (e.g. USDC's March 2023 depeg lasted hours), and mint+redeem require no cooldown, the arbitrage window is realistically exploitable by anyone monitoring price feeds and racing the PSM admin's response.

### Recommendation
- Do not treat all approved external assets in a PSM instance as fungible substitutes for one another at 1:1 par without a market-price reference. Either isolate each external asset's reserve so it can only ever be redeemed 1:1 against internal asset that was minted from that specific external (i.e., disallow cross-asset redemption within a shared internal debt pool), or integrate a price oracle (as LevelMinting's fix did) to discount mint output / redemption output based on live asset price.
- Add an automatic, price-triggered circuit breaker (or a maximum per-block/per-account swap-volume cap) rather than relying solely on manual admin intervention via `set_asset_status`.
- Consider enforcing a minimum fee floor and/or a cooldown between `mint` and `redeem` for the same account to remove the "fee-less" atomic-swap property that both this report and the referenced LevelMinting finding identify as the root cause.

### Proof of Concept
Given a PSM instance registered for internal asset `pUSD` with two approved externals, `USDC` and `USDT`, both currently priced at par per pallet assumptions, and existing `PsmDebt[pUSD, USDC] > 0` from prior honest usage:

1. `USDT` depegs to $0.50 in the open market (external event, not attacker-controlled).
2. Attacker calls:
```rust
Psm::mint(
    RuntimeOrigin::signed(attacker),
    PUSD_ASSET_ID,
    USDT_ASSET_ID,
    1_000 * USDT_UNIT,   // real market value ≈ $500
    Permill::from_percent(0), // minting fee can be 0%
)?;
```
This credits the attacker ≈ 1,000 `pUSD` (minus minimal fee) while only depositing USDT worth ≈$500, per [7](#0-6) .

3. In the same or a subsequent transaction, attacker calls:
```rust
Psm::redeem(
    RuntimeOrigin::signed(attacker),
    PUSD_ASSET_ID,
    USDC_ASSET_ID,
    1_000 * INTERNAL_UNIT,
    Permill::from_percent(1), // small redemption fee cap
)?;
```
This burns the attacker's `pUSD` and pays out ≈1,000 `USDC` (real value ≈$1,000) from the shared reserve, limited only by `PsmDebt[pUSD, USDC]` and reserve balance, per [8](#0-7) .

Net result: the attacker converted ≈$500 of depegged USDT into ≈$1,000 of USDC risk-free (minus negligible fees), and the PSM reserve is left holding more of the depegged USDT and correspondingly less USDC to back the remaining `pUSD` in circulation — reproducing the exact "LevelMinting can be arbitraged" scenario locally in `pallet-psm`.

### Citations

**File:** substrate/frame/psm/src/lib.rs (L42-62)
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

**File:** substrate/frame/psm/src/lib.rs (L716-754)
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
```

**File:** substrate/frame/psm/src/lib.rs (L811-855)
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
```

**File:** substrate/frame/psm/README.md (L1-20)
```markdown
# PSM Pallet

A module hosting one or more Peg Stability Modules. Each PSM enables 1:1 swaps
between a specific internal stablecoin and that PSM's pre-approved external
assets on Substrate-based blockchains.

## Terminology

Throughout this pallet two distinct token roles are referenced:

- **Internal** — the stablecoin a PSM issues and burns (e.g. runtime's own USD-pegged stablecoin).
  Each PSM instance is keyed by its internal asset id; multiple instances can
  coexist, each with its own reserve, debt ceiling, fee destination and
  approved externals. Mint operations credit the user with the internal asset;
  redeem operations burn it. Fees are collected in the internal asset and
  forwarded to that instance's `PsmInfo::fee_destination`.
- **External** — third-party assets (e.g. USDC, USDT) approved on a
  specific PSM via `add_external_asset` and held in that PSM's reserve. Users
  deposit external to mint internal, and burn internal to redeem external. A
  PSM may approve multiple externals, each identified by `asset_id`.
```

**File:** prdoc/stable2606/pr_11529.prdoc (L12-18)
```text
    Runtime changes:
    - Configures `pallet-psm` on Asset Hub Westend with pUSD (asset ID 50000342)
    - Adds `pallet-parameters` for governance-configurable maximum issuance (default 50 million pUSD)
    - Fee destination is the pUSD insurance fund account (`PalletId(*b"pusd/ins")`)
    - Adds V1 migration to initialize USDT (1984) as the first external asset
      with 0% minting fee and 0.01% redemption fee
    - Adds weights for the PSM pallet
```
