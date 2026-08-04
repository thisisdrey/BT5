### Title
Redeem path skips live-decimals validation while mint enforces it, allowing PSM reserve drain when an approved asset's decimals metadata changes - ([File: substrate/frame/psm/src/lib.rs])

### Summary
The `pallet-psm` "Peg Stability Module" is a local analog of the external `priceFeeds`-cannot-be-changed report: instead of an oracle price that cannot be refreshed, the PSM snapshots each approved asset's `decimals` at registration time and uses that snapshot as the implicit conversion rate between the internal and external stablecoin. The `mint` path re-validates the snapshot against live decimals before every swap via `Self::ensure_decimals_match`, but the `redeem` path deliberately skips this check and trusts the stale snapshot unconditionally. If an external (or internal) asset's live decimals metadata is later changed by its owner/issuer, `redeem` will keep computing conversions with the wrong scaling factor, letting a user extract far more real value from the PSM reserve than they burned in internal asset.

### Finding Description
`mint` calls `Self::ensure_decimals_match` [1](#0-0)  which re-reads live decimals from the fungibles backend and errors with `DecimalsMismatch` if they diverge from the snapshot stored in `ExternalAssetInfo::decimals` / `PsmInfo::internal_decimals` [2](#0-1) .

`redeem`, by contrast, reads the snapshot values directly without calling `ensure_decimals_match`: [3](#0-2) 

The conversion helpers `external_to_internal`/`internal_to_external` compute a `10^diff` scaling factor purely from the two decimals values passed in [4](#0-3) ; they have no way to detect that the snapshot no longer matches the token's real precision. If the external asset's `decimals` metadata is later changed (assets pallets allow the asset `Owner`/team to call `set_metadata` and change `decimals` after creation — this is unrelated to and not gated by the PSM), the redeem path will silently apply the old, now-incorrect scaling factor while transferring real balances via `T::Fungibles::transfer` [5](#0-4) .

Because raw balances (not decimal-adjusted "dollar" values) are what actually move, a decimals change shifts the real-world value represented by one raw unit. Using the stale scaling factor in `internal_to_external` then produces an `external_out` that no longer corresponds to the amount of internal asset burned, while `PsmDebt` bookkeeping is decremented using the same stale, self-consistent (but wrong) `effective_internal_net` [6](#0-5) . The only guard is `reserve < external_out` triggering `Error::Unexpected` [7](#0-6) , which only prevents redemption from failing outright once the reserve is drained below what stale accounting demands — it does not stop the drain from happening up to that point.

This is a direct analog of the reported bug class: a critical conversion parameter ("price"/decimals ratio) is snapshotted once and one code path (`mint`) actively guards against it going stale, while the other economically-symmetric path (`redeem`) does not, leaving an inconsistent, exploitable trust assumption baked into public, unprivileged extrinsics.

### Impact Explanation
An attacker who can cause (or simply waits for) a decimals metadata change on any approved external asset — a routine, non-privileged-to-the-PSM administrative action taken by that asset's own issuer/team, not by PSM or chain governance — can call `redeem` repeatedly to extract external reserve funds at an incorrect (favorable) rate, draining the PSM's `psm_account` reserve. This directly violates the "public underpriced work" / "theft of unbacked value" / "duplicate or wrong-amount settlement" categories in scope: reserve funds are moved to the wrong (over-large) amount for the internal asset actually burned, breaking the 1:1 backing invariant the whole PSM design depends on (`do_try_state`'s check `reserve >= debt_as_external` [8](#0-7)  would then fail, evidencing loss of solvency).

### Likelihood Explanation
Exploitability depends on an external asset's decimals metadata actually changing after PSM registration — this requires the asset's own owner/team to call something like `set_metadata`, which is outside the PSM's control. For assets whose team is untrusted, careless, or compromised (e.g. many permissionlessly-created assets on Asset Hub), this is realistically reachable without any PSM admin/governance action, and the resulting `redeem` calls are fully public/unprivileged. The asymmetry between `mint`'s defensive check and `redeem`'s lack thereof is a clear, provable code-level inconsistency rather than a hypothetical.

### Recommendation
Call `Self::ensure_decimals_match` (or an equivalent live-decimals check) in `redeem` before computing `internal_to_external`/`PsmDebt` mutations, mirroring `mint`. If intentionally allowing redemption to unwind stale positions is desired, gate that behavior behind an explicit, rate-limited, or governance-supervised path rather than silently trusting decimals that can diverge from the live asset metadata for the unprivileged public `redeem` entry point.

### Proof of Concept
1. Governance creates a PSM with `internal_decimals = 18` and approves external asset `X` with live decimals `6` via `add_external_asset`, snapshotting `ExternalAssetInfo.decimals = 6`.
2. Users mint/redeem normally; PSM reserve holds `X` 1:1 backing outstanding internal debt.
3. `X`'s asset team calls `pallet-assets::set_metadata` (or equivalent) to change `X`'s live decimals from `6` to `0` (a fully legitimate call from their perspective, unrelated to the PSM).
4. Attacker calls `Psm::redeem(internal_asset, X, internal_amount, max_fee)`. `redeem` reads the stale `external.decimals = 6` from storage (no live check), computes `external_out = internal_to_external(internal_net, 6, 18)`, i.e. divides by `10^12` as before.
5. Because `X` now actually has `0` decimals, each raw unit of `X` is worth `10^6` times more than at registration time; the attacker receives raw `X` balance computed under the old (wrong) scale, extracting `10^6`× the intended real value from the reserve for the internal asset burned.
6. Reserve is drained until `reserve < external_out` starts tripping `Error::Unexpected`, by which point the PSM is insolvent relative to `PsmDebt` for other holders.

### Citations

**File:** substrate/frame/psm/src/lib.rs (L716-717)
```rust
			let (ext_decimals, internal_decimals) =
				Self::ensure_decimals_match(&info, &internal_asset, &external_asset, &external)?;
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

**File:** substrate/frame/psm/src/lib.rs (L1601-1624)
```rust
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

**File:** substrate/frame/psm/src/lib.rs (L1636-1651)
```rust
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
