## Analysis

The Aave/PoolTogether bug's core invariant is: **a contract computes downstream state (shares to mint) from a *nominal* transfer amount instead of the *actual* balance delta observed after the transfer**, letting a divergence between requested and received value corrupt accounting.

The closest verified local analog is `pallet_psm::Pallet::mint` in the Peg Stability Module pallet.

### Title
Unbacked mint in PSM `mint()` — internal debt/mint amount derived from nominal `external_amount`, not from the PSM reserve's actual balance increase - (File: `substrate/frame/psm/src/lib.rs`)

### Summary
`Pallet::mint` computes how much internal stablecoin to mint, and how much debt to record, purely from the caller-supplied `external_amount` parameter and the resulting `effective_external`/`internal_equivalent` derived via decimal conversion. It never re-reads the PSM reserve account's actual balance before/after the `T::Fungibles::transfer` call to confirm that the reserve genuinely increased by `effective_external`. This is the same "trust the transferred-in amount instead of verifying contract balance increase" defect flagged in the external report.

### Finding Description
In `mint` [1](#0-0) , the flow is:
1. `internal_equivalent` is derived from `external_amount` (the raw user-supplied argument).
2. `effective_external` is the round-tripped external amount that is actually transferred.
3. `T::Fungibles::transfer(external_asset, &who, &psm_account, effective_external, Preservation::Expendable)?` moves the external asset into the PSM reserve [2](#0-1) .
4. Immediately after, `T::Fungibles::mint_into(internal_asset, &who, internal_to_user)` mints the internal stablecoin, and `PsmDebt` is incremented by `internal_equivalent` — both computed *before* the transfer, not from any post-transfer balance check [3](#0-2) .

The module's own documentation states the debt-tracking invariant: "PSM Debt: Total internal asset minted through a PSM, backed 1:1 by external assets in that PSM's reserve" [4](#0-3) . Nothing in `mint` enforces that invariant against the *actual* observed reserve increase — exactly the guard the external report's `supplyTokenTo` fix adds (`balanceBefore`/`balanceAfter` diff before minting shares).

`T::Fungibles` is bound only to the generic `fungibles::{Inspect, Mutate}` traits [5](#0-4) , so any runtime is free to wire the PSM's external-asset leg to a non-vanilla fungibles implementation (e.g. a foreign/bridged/wrapped asset type, an asset combined via `fungibles::UnionOf`, or an asset subject to holds/freezes/precision loss in its `transfer` implementation) whose actual balance delta can differ from the requested `effective_external`. In that case the pallet still credits the user with `internal_to_user` and books `internal_equivalent` of debt as if the reserve received the full nominal amount.

### Impact Explanation
If the configured `T::Fungibles` backend for any external asset does not guarantee an exact 1:1 transfer of the requested amount into the PSM reserve account, an attacker can mint more of the internal stablecoin than is actually backed in the reserve, i.e. an **unbacked mint**. This directly compromises the pallet's peg-backing guarantee, degrading the reserve's actual collateralization below the tracked `PsmDebt`, and can be repeated to drain redeemability for other holders (later redeemers cannot get external assets because the reserve was never fully funded).

### Likelihood Explanation
This requires an external-asset `Fungibles` implementation whose `transfer` does not deliver the exact requested amount to the destination (comparable to a fee-on-transfer/rebasing/deflationary token in the original report). This is not achievable with the standard `pallet-assets` backend alone (which transfers exact amounts), so the concrete exploitability depends on which `Fungibles` implementation a given runtime plugs into `pallet_psm::Config::Fungibles` for its external assets — this could not be fully confirmed from the indexed runtime wiring for the `node`/`rococo` runtimes referencing `pallet_psm` within the available search results, so likelihood is uncertain and configuration-dependent rather than proven exploitable against pallet-assets-only deployments.

### Recommendation
Mirror the report's fix pattern: read `T::Fungibles::balance(external_asset, &psm_account)` before and after the `transfer` call inside `mint` (and symmetrically for `redeem`'s transfer out of reserve), and use the actual observed delta — not the nominal `effective_external`/`internal_equivalent` — to compute `internal_to_user`, `fee`, and the `PsmDebt` increment. Alternatively, restrict `T::Fungibles`'s external-asset implementations to ones formally guaranteed to always move the exact requested amount, and enforce that guarantee at pallet-config time.

### Proof of Concept
Conceptual (blocked on confirming a live runtime that instantiates `pallet_psm::Config::Fungibles` over a non-exact-transfer asset backend, which could not be verified from the indexed code):
1. Runtime wires PSM's `external_asset` leg to a `Fungibles` implementation where `transfer(from, psm_account, amount)` delivers `amount - tax` to `psm_account` (e.g. a wrapped/bridged asset with a transfer fee, or one subject to an on-transfer hold/burn).
2. Attacker calls `mint(internal_asset, external_asset, external_amount, 0)`.
3. `effective_external` leaves the attacker's account, but the PSM reserve (`psm_account`) only receives `effective_external - tax`.
4. `internal_to_user` is minted and `PsmDebt` incremented as if the reserve received the full `effective_external`, permanently under-collateralizing the PSM by `tax` per mint — repeatable to build up unbacked internal-asset supply.

Given the uncertainty about which concrete `Fungibles` backend production runtimes attach to `pallet_psm`, this should be validated with a Devin session inspecting the full runtime configuration (search wasn't able to conclusively locate the `impl pallet_psm::Config for Runtime` block's `type Fungibles = ...` binding within the indexed context).

### Citations

**File:** substrate/frame/psm/src/lib.rs (L59-61)
```rust
//! * **Reserve**: External asset balance held by a PSM's reserve account (derived, not stored).
//! * **PSM Debt**: Total internal asset minted through a PSM, backed 1:1 by external assets in that
//!   PSM's reserve.
```

**File:** substrate/frame/psm/src/lib.rs (L132-139)
```rust
	use frame_support::{
		pallet_prelude::*,
		traits::{
			fungibles::{
				metadata::Inspect as FungiblesMetadataInspect,
				roles::Inspect as FungiblesRolesInspect, Inspect as FungiblesInspect,
				Mutate as FungiblesMutate,
			},
```

**File:** substrate/frame/psm/src/lib.rs (L702-756)
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
			if !fee.is_zero() {
				T::Fungibles::mint_into(internal_asset.clone(), &info.fee_destination, fee)?;
			}

			PsmDebt::<T>::insert(&internal_asset, &external_asset, new_debt);
```
