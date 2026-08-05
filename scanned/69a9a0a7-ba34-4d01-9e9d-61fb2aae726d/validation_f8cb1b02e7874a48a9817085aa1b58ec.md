## Analysis

The Berachain HoneyFactory bug is about a stablecoin-minting module that treats a set of "approved collateral" assets as always worth par value, with peg-safety enforced only reactively (an `isBasketModeEnabled()` scan or admin intervention) rather than atomically at mint time. I found a structurally identical pattern in this repository's Peg Stability Module pallet. [1](#0-0) 

The `mint` extrinsic converts any approved `external_asset` into `internal_asset` purely via decimal-count conversion, with **no oracle, no price check, no automatic peg-deviation detection at all** — protection depends entirely on an admin noticing a depeg and manually calling `set_asset_status` to disable that external: [2](#0-1) 

The instance-wide debt ceiling (`total_psm_debt`, `max_asset_debt`) only limits *quantity* of internal asset minted against an external — it never validates that the external asset is actually worth what the pallet assumes ($1, via `external_to_internal`/`internal_to_external` decimal-only conversion). The only mitigation is the manually-set `CircuitBreakerLevel` per external asset, set via `set_asset_status`: [3](#0-2) 

This is exactly the Honey bug's root cause and recommendation transplanted: "do not mint with a depegged asset ... or halt minting until admin can intervene" — here there isn't even an automatic `isBasketModeEnabled()`-style detection; the pallet has zero automated depeg detection, so the window of exploitable unbacked minting is open indefinitely until an admin manually reacts.

### Title
Unbacked 1:1 minting against a depegged external asset with no oracle or peg-deviation check in PSM `mint` - (File: `substrate/frame/psm/src/lib.rs`)

### Summary
`Pallet::mint` in the PSM pallet always converts `external_amount` of an approved `external_asset` into `internal_asset` at a decimal-adjusted 1:1 rate, without any check that the external asset is actually trading at parity. If any approved external asset (e.g. a stablecoin) depegs — through a hack, bad backing, or unlimited minting on its own issuing chain/parachain — a user can deposit worthless (or heavily discounted) tokens and receive full-value internal stablecoin, exactly as in the Berachain HoneyFactory report. The only safeguard is the `CircuitBreakerLevel` set by `set_asset_status`, which is a manual, reactive admin action, not a pre-emptive check inside `mint`.

### Finding Description
`mint()` [2](#0-1)  performs:
1. `ensure!(external.status.allows_minting(), ...)` — only blocks minting if an admin has *already* flipped the circuit breaker.
2. `external_to_internal` / `internal_to_external` — pure decimal-precision conversion, assuming the external asset is worth exactly the same as the internal asset.
3. Aggregate (`total_psm_debt`) and per-asset (`max_asset_debt`) ceilings — these bound *quantity*, not *value*; they do not detect a depeg.

There is no oracle, no price feed, and no automatic peg-deviation detection anywhere in the pallet (confirmed by the absence of any peg/oracle/price logic outside the doc comments). This is weaker than the Honey contract's `isBasketModeEnabled()`, which at least auto-detects depegs via Pyth price bounds; here the pallet has *no* automatic detection mechanism at all — it relies solely on `set_asset_status` being called manually after the fact [3](#0-2) .

### Impact Explanation
An unprivileged user can mint `internal_asset` (the runtime's own stablecoin) 1:1 against an approved `external_asset` that has depegged to near-zero value, up to the per-asset and aggregate debt ceilings (`ExceedsMaxPsmDebt` guards only bound the *amount*, not the *value*, of debt taken on). This directly produces unbacked mint of the internal stablecoin, corrupting the invariant documented in the pallet ("PSM Debt: Total internal asset minted through a PSM, backed 1:1 by external assets in that PSM's reserve" [4](#0-3) ). Every legitimate holder of the internal stablecoin is diluted since the pooled reserve is no longer fully backed, and the loss is realized whenever depositors of other externals attempt to redeem before the admin reacts.

### Likelihood Explanation
No malicious admin, governance actor, validator, collator, relayer, or leaked key is required. Any already-approved external asset (a normal onboarding decision, not an admin abuse) can depeg for reasons entirely outside PSM's control (its own chain's exploit, unlimited mint bug, bridge failure, etc.). The attack requires nothing more than an unprivileged signed account calling `mint` while that condition holds and before `set_asset_status` is manually invoked — an unbounded window since there is no automatic trigger.

### Recommendation
Do not rely solely on a manually-set circuit breaker. Either:
- integrate a price/oracle check (with staleness bounds) into `mint()` that rejects or discounts minting when the external asset trades outside a configured peg band, or
- automatically flip the circuit breaker to `MintingDisabled`/`AllDisabled` when an external asset's tracked price deviates beyond a threshold, mirroring — and improving on — the very fix Berachain applied on `honey/v2` (auto-detect depeg and halt minting immediately rather than trusting decimal-only 1:1 conversion).

### Proof of Concept
1. PSM instance for `internal_asset` has two approved externals: `USDC` (healthy) and `X` (an external asset that later depegs to near-zero real value, e.g. due to an exploit on its issuing chain).
2. Attacker calls `Psm::mint(origin, internal_asset, X, external_amount, max_fee)` [5](#0-4) , depositing a large amount of now-worthless `X`.
3. `external_to_internal` converts this 1:1 by decimals only [6](#0-5) ; ceilings (`ExceedsMaxPsmDebt`) only check amount versus `max_debt`/`max_asset_debt`, not real value [7](#0-6) .
4. Attacker receives full-value `internal_asset` for worthless `X`, before any admin calls `set_asset_status(internal_asset, X, AllDisabled)` [3](#0-2) .
5. Attacker immediately redeems the freshly minted `internal_asset` against `USDC` (a different, still-healthy external on the same instance) or sells it, realizing value backed by nothing, leaving the instance under-collateralized relative to its outstanding internal-asset debt.

### Citations

**File:** substrate/frame/psm/src/lib.rs (L18-22)
```rust
//! # Peg Stability Module (PSM) Pallet
//!
//! Instantiable Peg Stability Modules (PSMs). Each PSM enables 1:1 swaps between an internal
//! stablecoin and one or more approved external stablecoins, typically to maintain a peg.
//!
```

**File:** substrate/frame/psm/src/lib.rs (L60-61)
```rust
//! * **PSM Debt**: Total internal asset minted through a PSM, backed 1:1 by external assets in that
//!   PSM's reserve.
```

**File:** substrate/frame/psm/src/lib.rs (L702-741)
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
```

**File:** substrate/frame/psm/src/lib.rs (L1210-1225)
```rust
		pub fn set_asset_status(
			origin: OriginFor<T>,
			internal_asset: T::AssetId,
			external_asset: T::AssetId,
			status: CircuitBreakerLevel,
		) -> DispatchResult {
			Self::ensure_psm_admin(origin, &internal_asset, |l| l.can_set_circuit_breaker())?;
			ExternalAssets::<T>::try_mutate(
				&internal_asset,
				&external_asset,
				|maybe| -> DispatchResult {
					let info = maybe.as_mut().ok_or(Error::<T>::AssetNotApproved)?;
					info.status = status;
					Ok(())
				},
			)?;
```
