## Local Analog Found: Inconsistent decimals validation between `mint` and `redeem` in `pallet-psm`

### Title
Stale/unverified decimals snapshot used in `redeem` lets external-asset decimal drift break 1:1 peg accounting and drain the PSM reserve - (File: `substrate/frame/psm/src/lib.rs`)

### Summary
`pallet-psm` mints/redeems an internal stablecoin against approved external assets, converting between them with a scaling factor derived from each asset's `decimals` metadata. `mint` re-validates that live decimals still match the registration-time snapshot before every swap via `ensure_decimals_match`, but `redeem` deliberately skips this validation and uses the stored (possibly stale) snapshot values directly. Because ERC‑4626-style “decimals” fields are purely metadata and do not rescale the underlying raw balances, any drift between the snapshot and the live value corrupts the peg's conversion math on the redemption path only, breaking the 1:1 value-conservation invariant the whole pallet is built around.

### Finding Description
In `mint`, decimals are checked on every call: [1](#0-0) 

`ensure_decimals_match` compares the live `T::Fungibles::decimals` of both the external and internal asset against the values snapshotted in `ExternalAssetInfo::decimals` / `PsmInfo::internal_decimals`, rejecting the mint with `Error::DecimalsMismatch` if they diverge: [2](#0-1) 

`redeem`, however, reads the snapshot values directly and never calls `ensure_decimals_match`: [3](#0-2) 

This asymmetry is explicit in the doc comment: *"Redemptions use the decimals snapshotted when the PSM/external pair was registered, allowing existing positions to unwind even if live metadata later changes."* That is: the pallet knowingly permits `redeem` to keep using outdated decimal-scaling factors indefinitely, with no upper bound on how stale the snapshot may become or how large the resulting mis-scaling can be.

Decimals in the fungibles metadata trait are display-only; they don't rescale on-chain raw balances. `external_to_internal` / `internal_to_external` treat the decimals difference as the actual unit-conversion factor between the internal stablecoin and the external asset (via `10^diff` multiply/divide): [4](#0-3) [5](#0-4) 

Any external asset's owner can permissionlessly change that asset's `decimals` field after the PSM has approved it (a normal, non-privileged asset-management action, not an admin/governance action on the PSM itself). Once decimals drift:
- `mint` immediately halts for that pair (`DecimalsMismatch`), so new debt cannot accrue at the wrong rate.
- `redeem` keeps operating on the frozen, now-incorrect snapshot `external.decimals`/`info.internal_decimals`, so `internal_to_external` computes `external_out` using a scaling factor that no longer reflects the asset's true unit representation relative to the internal stablecoin's peg assumption.

Because the amount actually transferred out of the reserve (`external_out`) is driven purely by this stale factor, an attacker can arrange (or simply exploit organically occurring) decimals drift to redeem disproportionately more external collateral per unit of internal asset burned than the reserve was ever backed for — i.e., unbacked withdrawal from the PSM reserve — while `PsmDebt` bookkeeping still assumes 1:1 backing was maintained.

### Impact Explanation
This directly violates the "Balances, assets... must conserve value and settle exactly once to the rightful beneficiary and amount" invariant. Exploiting the stale conversion factor on `redeem` lets a user extract external-asset reserve funds in excess of what their burned internal stablecoin actually represents, i.e., an unbacked drain of PSM collateral — a genuine value-loss bug rather than a cosmetic display mismatch (unlike the original ERC4626 report). It can also work in the opposite direction and effectively lock legitimate redeemers out of their fair share of the reserve if the drift makes `internal_to_external` round to a smaller payout, harming honest users.

### Likelihood Explanation
Medium: the trigger condition (an external asset's live `decimals` metadata changing after PSM approval) is a normal, permissionless action by that asset's owner via the standard fungibles/assets metadata call, not a PSM-admin/governance action and not requiring a malicious validator, relayer, or node. `mint`'s guard proves the pallet authors are aware decimals can drift and treat it as a security-relevant condition, but the same guard was deliberately omitted for `redeem`, leaving the exploit window open for as long as an external asset remains approved with any decimals drift.

### Recommendation
Apply the same live-decimals validation in `redeem` as in `mint` (call `ensure_decimals_match` before computing `external_out`), or, if the "let stale positions unwind" behavior is intentional, bound it: freeze/disable redemption entirely for a pair once decimals diverge (mirroring `MintingStopped`/`AllSwapsStopped`) rather than silently continuing to apply a stale scaling factor to fund transfers. At minimum, cap or checkpoint the maximum drift tolerated and require governance re-registration (fresh snapshot) before further redemptions are permitted for that pair.

### Proof of Concept
1. Governance creates a PSM for `internal_asset` (18 decimals) and approves `external_asset` (currently 6 decimals) via `add_external_asset`; snapshot stored: `ExternalAssetInfo.decimals = 6`.
2. Several users `mint` internal stablecoin against the external asset, building up reserve and `PsmDebt` under the 6-decimals conversion factor.
3. The external asset's owner (unprivileged w.r.t. the PSM) calls the assets pallet's metadata-update extrinsic to change `external_asset`'s decimals from 6 to, e.g., 0 (a legitimate, permissionless action for an asset owner). Raw balances of the asset are untouched.
4. Any further `mint` for this pair now fails with `Error::DecimalsMismatch` (per `ensure_decimals_match`), so no protection is bypassed there.
5. However, `redeem` still succeeds: it reads `external.decimals = 6` (stale) and `info.internal_decimals = 18` from storage without re-checking against the now-different live decimals, and computes `external_out = internal_to_external(internal_net, 6, 18)`.
6. Because the true unit relationship between the assets is no longer what "6 vs 18 decimals" implies (the asset owner changed the metadata precisely to misrepresent it), the caller redeems an external amount inconsistent with the actual 1:1 peg the reserve was funded under, extracting more collateral than their burned internal debt should entitle them to — while `PsmDebt` is decremented as if the trade were still fairly priced, silently under-collateralizing the remaining internal-asset supply for that pair.

### Citations

**File:** substrate/frame/psm/src/lib.rs (L712-720)
```rust
			let external = ExternalAssets::<T>::get(&internal_asset, &external_asset)
				.ok_or(Error::<T>::UnsupportedAsset)?;
			ensure!(external.status.allows_minting(), Error::<T>::MintingStopped);

			let (ext_decimals, internal_decimals) =
				Self::ensure_decimals_match(&info, &internal_asset, &external_asset, &external)?;

			let internal_equivalent =
				Self::external_to_internal(external_amount, ext_decimals, internal_decimals)?;
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

**File:** substrate/frame/psm/src/lib.rs (L1580-1599)
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
