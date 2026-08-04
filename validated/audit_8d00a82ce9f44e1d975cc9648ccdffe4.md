### Title
Redemption path uses stale decimals snapshot instead of live metadata, unlike the minting path - (File: substrate/frame/psm/src/lib.rs)

### Summary
`pallet-psm` implements a Peg Stability Module — the same "1:1 swap to defend a peg" primitive as Ditto's oracle-gated shorts/redemptions. The `mint` extrinsic validates that live asset decimals still match the registration-time snapshot before computing conversion amounts, but the `redeem` extrinsic deliberately skips that validation and always uses the stale snapshot values (`external.decimals`, `info.internal_decimals`). This is the same asymmetry as the Ditto finding: one action path (liquidation / mint) refreshes/validates the pricing input before use, the other (redemption / redeem) consumes a cached value directly, letting the conversion rate diverge from ground truth on the path that settles value.

### Finding Description
In `mint`, before any amount is computed, the pallet calls `Self::ensure_decimals_match(&info, &internal_asset, &external_asset, &external)?` to confirm the live decimals reported by `T::Fungibles::decimals(...)` still equal the values snapshotted in `PsmInfo::internal_decimals` and `ExternalAssetInfo::decimals` at registration time: [1](#0-0) 

In `redeem`, the same two decimal values are read directly from storage with no live-vs-snapshot check at all: [2](#0-1) 

The pallet's own doc comment confirms this is intentional but does not change the fact that it reintroduces the "cached vs. fresh" asymmetry the external report warns about: mint treats the snapshot as untrustworthy unless revalidated, redeem treats it as always authoritative: [3](#0-2) 

`ExternalAssetInfo::decimals` and `PsmInfo::internal_decimals` are one-time snapshots taken at `add_external_asset` / `create_psm` time: [4](#0-3) [5](#0-4) 

The underlying `T::Fungibles` metadata (asset decimals) is not immutable in the general case — `pallet-assets`-style backends allow the asset owner to update metadata (including decimals) after creation via `set_metadata`. If live decimals drift from the snapshot (whether through legitimate metadata correction or an asset owner unilaterally changing it), `mint` will hard-fail with `DecimalsMismatch` and stop growing the PSM's debt on that pair, exactly like Ditto's liquidation path refusing to act on stale pricing. `redeem`, however, keeps computing `internal_to_external`/`external_to_internal` scaling with the old decimals exponent (`10^diff`), silently mis-pricing every unit burned against the reserve while the exit door stays open. This scaling factor is a magnitude-order (power-of-ten) quantity, so even a one-decimal drift changes payouts by 10x, either draining the reserve (user extracts far more external asset per internal asset burned than the peg intends) or shortchanging redeemers (protocol keeps external collateral it no longer owes), while `PsmDebt` bookkeeping is adjusted using the same wrong exponent, corrupting the tracked debt invariant that ties `PsmDebt` to the actual reserve held.

### Impact Explanation
This directly hits the "conserve value and settle exactly once at correct amount" pivot for asset accounting: a divergence between mint-time validated decimals and redeem-time trusted decimals lets redemptions settle at the wrong exchange rate without any guard, either draining a PSM's external reserve (fund loss) or systematically underpaying redeemers (locked/lost user value), and corrupts `PsmDebt` tracking used to gate further mint/redeem activity across the instance.

### Likelihood Explanation
Requires the live decimals of an already-approved external or internal asset to diverge from its PSM-registration snapshot. This is plausible under normal (non-privileged, non-malicious-relayer) operation whenever the asset's own admin/issuer legitimately updates its metadata after the PSM already approved it — no PSM governance action or validator/relayer misbehavior is needed, only an unprivileged asset issuer performing a routine metadata edit unrelated to the PSM. Because `mint` actively defends against this with `ensure_decimals_match` while `redeem` does not, the asymmetry is a design gap rather than a one-off oversight, matching the "confirmed" severity pattern of the original Ditto report.

### Recommendation
Apply the same `ensure_decimals_match` (or equivalent live-vs-snapshot) check in `redeem` that `mint` already performs, or explicitly re-derive/re-validate decimals against current `T::Fungibles` metadata before using them to scale amounts and adjust `PsmDebt`. If intentionally allowing "unwind at old rate" is desired, gate it behind an explicit, bounded fallback (e.g. only after the pair has been flagged for removal) rather than unconditionally trusting the stale snapshot on every redemption.

### Proof of Concept
1. `create_psm` for `internal_asset` with `internal_decimals` snapshotted (e.g. 6) via `T::Fungibles::decimals`.
2. `add_external_asset` approves `external_asset` and snapshots its decimals (e.g. 6) into `ExternalAssetInfo::decimals`.
3. The external asset's issuer calls `set_metadata` (or equivalent) on the underlying fungibles pallet to change the asset's live decimals to, say, 18 (a legitimate, unprivileged action by that asset's own admin, unrelated to PSM governance).
4. Any `mint` call now fails with `DecimalsMismatch` via `ensure_decimals_match`, halting new debt growth on the pair — the intended circuit-break.
5. `redeem` is called for the same pair: it reads `external.decimals` (6) and `info.internal_decimals` (6) from storage, computes `internal_to_external` using the stale 1:1 (10^0) scaling factor at lines 825-836, and transfers `external_out` at a rate that is now off by `10^12` relative to the asset's real live precision.
6. `T::Fungibles::transfer` succeeds because the raw balance amount computed is what's checked against the reserve — the mismatch is purely in interpreted "value," not in raw-unit accounting — so the redemption executes and settles the wrong amount of value, while `PsmDebt` is decremented using the same wrong `effective_internal_net`, corrupting the debt/reserve invariant tracked by the pallet: [6](#0-5)

### Citations

**File:** substrate/frame/psm/src/lib.rs (L288-292)
```rust
		/// Snapshot of the internal asset's decimals at install time.
		pub internal_decimals: u8,
		/// Number of approved external assets attached to this instance.
		pub external_count: u32,
	}
```

**File:** substrate/frame/psm/src/lib.rs (L325-330)
```rust
	pub struct ExternalAssetInfo {
		/// Per-external circuit breaker status.
		pub status: CircuitBreakerLevel,
		/// Snapshot of the external asset's decimals at registration time.
		pub decimals: u8,
	}
```

**File:** substrate/frame/psm/src/lib.rs (L712-721)
```rust
			let external = ExternalAssets::<T>::get(&internal_asset, &external_asset)
				.ok_or(Error::<T>::UnsupportedAsset)?;
			ensure!(external.status.allows_minting(), Error::<T>::MintingStopped);

			let (ext_decimals, internal_decimals) =
				Self::ensure_decimals_match(&info, &internal_asset, &external_asset, &external)?;

			let internal_equivalent =
				Self::external_to_internal(external_amount, ext_decimals, internal_decimals)?;
			ensure!(!internal_equivalent.is_zero(), Error::<T>::AmountTooSmallAfterConversion);
```

**File:** substrate/frame/psm/src/lib.rs (L781-783)
```rust
		/// undercharges. Redemptions use the decimals snapshotted when the PSM/external pair
		/// was registered, allowing existing positions to unwind even if live metadata later
		/// changes.
```

**File:** substrate/frame/psm/src/lib.rs (L821-836)
```rust
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
```

**File:** substrate/frame/psm/src/lib.rs (L845-891)
```rust
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
```
