### Title
`redeem()` skips the live-decimals drift check that `mint()` enforces, letting external-asset decimal changes desynchronize PSM accounting from real reserve value - ([File: substrate/frame/psm/src/lib.rs])

### Summary
`pallet-psm`'s `mint()` calls `Self::ensure_decimals_match()` before doing any decimal-scaling arithmetic, which re-reads the live `decimals()` of both the internal and external asset from `T::Fungibles` and reverts with `Error::DecimalsMismatch` if either has drifted from the snapshot stored in `PsmInfo`/`ExternalAssetInfo` at registration time. [1](#0-0) 
`redeem()`, however, never calls `ensure_decimals_match`, and instead reads the frozen snapshot values `external.decimals` / `info.internal_decimals` unconditionally and uses them for `internal_to_external` / `external_to_internal` scaling. [2](#0-1) 

### Finding Description
The External report's core broken invariant is: a public mint/settlement path converts between two asset representations using an assumed decimal relationship, but that relationship can diverge from the true one, letting a caller receive more (or effectively "free") value than deposited.

In `pallet-psm`, decimals for both the internal stablecoin and each approved external asset are snapshotted once (`PsmInfo::internal_decimals` at `create_psm`, `ExternalAssetInfo::decimals` at external-asset approval) and then used repeatedly to scale amounts between the two token representations via `external_to_internal` / `internal_to_external`. [3](#0-2) 

The pallet's own design (added specifically to guard against exactly this bug class - see the `DecimalsMismatch` PR) recognizes that live decimals metadata can drift after registration - `mint()` explicitly re-validates live decimals every call and halts with `DecimalsMismatch` if they no longer match the snapshot. [4](#0-3) 

But `redeem()` has no equivalent check anywhere in its body - it reads `let ext_decimals = external.decimals; let internal_decimals = info.internal_decimals;` straight from storage and proceeds directly into the scaling math and transfers, with no comparison to `T::Fungibles::decimals(...)`. [5](#0-4) 

Decimals metadata for an asset in `pallet-assets` (the typical `T::Fungibles` backend) is mutable by the asset's owner/admin via `set_metadata`, and is not enforced to be consistent with actual raw-unit economics of the token — it is pure metadata used by PSM only to derive its scaling factor. Because `mint()` treats any drift as fatal (halting further minting on that asset) but `redeem()` silently continues to use the stale, un-verified snapshot decimals, the two entry points are asymmetric: once a `DecimalsMismatch` occurs, minting for that external is permanently blocked while redemption keeps operating on values that no longer correspond to a verified decimals relationship. This breaks the "corrupted value" that must be bound and checked exactly once per settlement: `ext_decimals`, `internal_decimals`, used in `internal_to_external` at line 836 and `external_to_internal` at line 846, are trusted without being re-derived/re-validated against the live source of truth, unlike every other swap-relevant guard in the pallet (min swap amount, fee cap, debt ceiling, reserve sufficiency are all checked, but decimals correctness is not).

### Impact Explanation
If the true decimals of an external asset diverge from what was snapshotted at approval time (whether through owner-controlled metadata changes, migration bugs, or any other drift vector the pallet's own `DecimalsMismatch` guard was built to catch), `redeem()` will keep computing `external_out`/`effective_internal_net` using the wrong scaling factor. Depending on the direction of drift this either:
- Lets a redeemer extract more external-asset raw units per unit of internal-asset burned than the asset is actually worth, draining the PSM reserve (a `pallet-assets`-analog of "free mint" — value creation from a decimals mismatch), or
- Traps/underpays other redeemers once the reserve is drained relative to tracked debt, since `PsmDebt` bookkeeping (`effective_internal_net`) is also derived from the same stale ratio and the pallet's own invariant check (`do_try_state`) assumes reserve ≥ debt-as-external computed from `external.decimals`, which is exactly the value that becomes wrong. [6](#0-5) 

This directly matches the "theft or unbacked mint or unlock" / "asset accounting must conserve value" impact classes.

### Likelihood Explanation
The precondition — that live decimals for an approved external can diverge from the registration snapshot — is exactly the scenario the pallet's authors added `ensure_decimals_match`/`DecimalsMismatch` to defend against, which confirms it is considered a realistic, in-scope threat model for this pallet (not a theoretical one). `mint()` is fully hardened against it; `redeem()` is not. No malicious peer, validator, collator, or governance abuse is required — an ordinary external-asset owner/admin who can call `set_metadata` on their own asset (a routine, non-privileged pallet-assets action, not "leaked keys" or "compromised" anything) is enough to desynchronize the two decimal readings and then redeem against the stale figures.

### Recommendation
Call `Self::ensure_decimals_match(&info, &internal_asset, &external_asset, &external)?` at the start of `redeem()`, exactly as `mint()` does, before any scaling math is performed, so both entry points share one consistent, live-verified decimals invariant. If unwinding of existing positions during a legitimate drift is a desired product feature, that should be an explicit, separately-gated migration/admin path (e.g., a governance-triggered "wind-down" mode) rather than the default behavior of the public `redeem` extrinsic.

### Proof of Concept
1. Governance/admin approves external asset `E` on PSM instance for `internal_asset` `I` (6 decimals) with `E` snapshotted at `ext_decimals = 6` via `add_external_asset`-style flow (`ExternalAssetInfo::decimals`).
2. A user mints normally: deposits `E` 1:1, receives `I`. `PsmDebt` and PSM reserve reflect the true 6:6 relationship.
3. `E`'s owner/admin calls `pallet_assets::set_metadata` (or equivalent) on `E`, changing its decimals metadata to e.g. `2`. Live `T::Fungibles::decimals(E)` is now `2`, but `ExternalAssetInfo::decimals` snapshot is still `6`.
4. Any further `Psm::mint(..., E, ...)` call now reverts with `Error::DecimalsMismatch` via `ensure_decimals_match` — minting is correctly halted.
5. The same user calls `Psm::redeem(I, E, internal_amount, max_fee)`. `redeem()` never calls `ensure_decimals_match`; it uses `ext_decimals = 6` (stale) and `internal_decimals = 6` from storage directly to compute `external_out = internal_to_external(internal_net, 6, 6) = internal_net` (i.e., 1:1), transferring `E` out of the reserve at the stale ratio even though `E`'s live decimals metadata says `2`. The redemption succeeds and moves reserve funds using a decimals relationship that is no longer verified against the live asset, with no `DecimalsMismatch` guard blocking it. [7](#0-6) [8](#0-7)

### Citations

**File:** substrate/frame/psm/src/lib.rs (L716-717)
```rust
			let (ext_decimals, internal_decimals) =
				Self::ensure_decimals_match(&info, &internal_asset, &external_asset, &external)?;
```

**File:** substrate/frame/psm/src/lib.rs (L811-846)
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
```

**File:** substrate/frame/psm/src/lib.rs (L1580-1624)
```rust
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

		/// Convert an amount denominated in internal units into external-asset units.
		///
		/// Inverse of [`Self::external_to_internal`]. Floor-divides when internal has more
		/// decimals, multiplies up when it has fewer.
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

**File:** substrate/frame/psm/src/lib.rs (L1633-1651)
```rust
		/// Verify the live decimals for an external still match the snapshot taken at
		/// registration on this PSM, and that the internal asset's live decimals still
		/// match the snapshot stored in [`PsmInfo`].
		pub(crate) fn ensure_decimals_match(
			info: &PsmInfo<T>,
			internal_asset: &T::AssetId,
			external_asset: &T::AssetId,
			external: &ExternalAssetInfo,
		) -> Result<(u8, u8), DispatchError> {
			ensure!(
				T::Fungibles::decimals(external_asset.clone()) == external.decimals,
				Error::<T>::DecimalsMismatch
			);
			ensure!(
				T::Fungibles::decimals(internal_asset.clone()) == info.internal_decimals,
				Error::<T>::DecimalsMismatch
			);
			Ok((external.decimals, info.internal_decimals))
		}
```

**File:** substrate/frame/psm/src/lib.rs (L1693-1702)
```rust
					// 1. Per-external reserve covers tracked debt.
					let debt = PsmDebt::<T>::get(&internal_asset, &external_asset);
					let reserve = Self::get_reserve(&internal_asset, &external_asset);
					let debt_as_external =
						Self::internal_to_external(debt, external.decimals, info.internal_decimals)
							.map_err(|_| "Failed to convert tracked debt to external units")?;
					ensure!(
						reserve >= debt_as_external,
						"PSM reserve is less than tracked debt for an asset"
					);
```
