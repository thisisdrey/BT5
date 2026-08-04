### Title
`redeem` in `pallet-psm` uses a stale decimals snapshot without live-metadata verification, unlike `mint` - ([File: substrate/frame/psm/src/lib.rs])

### Summary
`pallet-psm` normalizes between an internal stablecoin and approved external assets by scaling amounts with `10^|ext_decimals - internal_decimals|` computed from decimals *snapshots* taken at asset-registration time (`ExternalAssetInfo::decimals`, `PsmInfo::internal_decimals`). The `mint` extrinsic explicitly re-validates these snapshots against the live `T::Fungibles::decimals(...)` values via `Self::ensure_decimals_match` before converting, and rejects the call with `Error::DecimalsMismatch` if they diverge. `redeem`, however, reads `external.decimals` and `info.internal_decimals` directly and performs the scaling conversion without ever calling `ensure_decimals_match` or otherwise checking the live decimals of either asset.

### Finding Description
The conversion helpers `external_to_internal` / `internal_to_external` (substrate/frame/psm/src/lib.rs:1575-1624) scale amounts purely based on the decimals values they are given — exactly the same class of bug as the Compound `CTokenMultiOracle` issue, where a scaling exponent was assumed correct instead of being confirmed against the authoritative source.

In `mint` (substrate/frame/psm/src/lib.rs:702-767), the pallet calls: [1](#0-0) 
which internally checks: [2](#0-1) 
i.e. it re-fetches `T::Fungibles::decimals(...)` for both assets and errors with `DecimalsMismatch` if they no longer match the values snapshotted in `PsmInfo`/`ExternalAssetInfo` at `create_psm`/`add_external_asset` time.

`redeem` (substrate/frame/psm/src/lib.rs:811-902) skips this entirely: [3](#0-2) 
It takes `ext_decimals`/`internal_decimals` straight from the stored structs and feeds them into `internal_to_external`/`external_to_internal` to compute `external_out` and the debt decrement `effective_internal_net`, with no consistency check against the live metadata.

`add_external_asset` snapshots `asset_decimals = T::Fungibles::decimals(external_asset.clone())` once at approval time: [4](#0-3) 
and `create_psm` snapshots `internal_decimals` once: [5](#0-4) 
Nothing in the pallet prevents these snapshots from later diverging from the live decimals reported by the `Fungibles` backend (e.g. `pallet-assets` metadata being altered, or, for assets whose decimals can otherwise be reconfigured post-registration by their own admin/owner path, independent of PSM governance). `mint` defends against this divergence; `redeem` does not, so a call to `redeem` after such a divergence silently uses the wrong scaling factor for the external-asset leg of the conversion.

### Impact Explanation
If the live decimals of the external or internal asset diverge from the PSM's stored snapshot (via any path outside the PSM's own admin control, e.g. re-registration of the underlying asset with different decimals, or an asset implementation whose `decimals()` is not immutable), `redeem` computes `external_out` and the debt-reduction amount `effective_internal_net` using the wrong power-of-ten scaling factor. This can pay out an external amount that is orders of magnitude larger or smaller than the correct one relative to the internal amount burned, misprice the PSM's reserve, and desynchronize `PsmDebt` accounting from the actual reserve balance — i.e. fund loss/lock or incorrect settlement amounts on a public, unprivileged entrypoint, consistent with "theft or unbacked mint/unlock" and "public underpriced work" impact classes.

### Likelihood Explanation
Exploitability depends entirely on whether the decimals of an already-approved asset can diverge from the snapshot taken at registration — this is a live-scope precondition outside `redeem`'s control that I could not fully confirm from the indexed code (e.g., whether `pallet-assets`'s metadata `decimals` field is mutable post-creation, or whether some non-`pallet-assets` `Fungibles` implementation could report changing decimals). The pallet's own defensive design (`ensure_decimals_match`, `DecimalsMismatch` error, and the prdoc's explicit mention of a "Runtime drift guard") indicates the authors considered decimals drift a real threat worth guarding against in `mint`; the asymmetric omission in `redeem` is the concrete, locally-provable gap.

### Recommendation
Call `Self::ensure_decimals_match(&info, &internal_asset, &external_asset, &external)?` in `redeem` before computing `internal_to_external`/`external_to_internal`, exactly as `mint` does, so that a live-decimals mismatch causes `redeem` to fail with `Error::DecimalsMismatch` rather than silently misscaling the payout.

### Proof of Concept
1. `create_psm` for `internal_asset` (decimals `D_i`), then `add_external_asset` approves `external_asset` (decimals `D_e`), snapshotting `ExternalAssetInfo::decimals = D_e` and `PsmInfo::internal_decimals = D_i`.
2. A user `mint`s to build up `PsmDebt` and reserve balance for that external asset (this call is protected by `ensure_decimals_match` and succeeds while decimals match).
3. The live decimals of `external_asset` (or `internal_asset`) subsequently change relative to the stored snapshot (via whatever external mechanism controls that asset's metadata, independent of the PSM pallet).
4. A user calls `redeem` for the same `(internal_asset, external_asset)` pair. `redeem` reads the *stale* `external.decimals`/`info.internal_decimals` (step 1's values) instead of the current live decimals, computes `external_out = internal_to_external(internal_net, stale_ext_decimals, stale_internal_decimals)` — off by a factor of `10^k` relative to the amount that would be correct under the asset's current decimals — and transfers that (wrong) amount out of the PSM reserve while decrementing `PsmDebt` by the correspondingly wrong `effective_internal_net`, all without any error being raised.

### Citations

**File:** substrate/frame/psm/src/lib.rs (L716-720)
```rust
			let (ext_decimals, internal_decimals) =
				Self::ensure_decimals_match(&info, &internal_asset, &external_asset, &external)?;

			let internal_equivalent =
				Self::external_to_internal(external_amount, ext_decimals, internal_decimals)?;
```

**File:** substrate/frame/psm/src/lib.rs (L825-826)
```rust
			let ext_decimals = external.decimals;
			let internal_decimals = info.internal_decimals;
```

**File:** substrate/frame/psm/src/lib.rs (L968-978)
```rust
			let internal_decimals = T::Fungibles::decimals(internal_asset.clone());
			Psm::<T>::insert(
				&internal_asset,
				PsmInfo::<T> {
					fee_destination: fee_destination.clone(),
					max_debt,
					min_swap_amount,
					internal_decimals,
					external_count: 0,
				},
			);
```

**File:** substrate/frame/psm/src/lib.rs (L1333-1350)
```rust
			let asset_decimals = T::Fungibles::decimals(external_asset.clone());
			ensure!(
				T::Fungibles::decimals(internal_asset.clone()) == info.internal_decimals,
				Error::<T>::DecimalsMismatch
			);
			ensure!(
				(asset_decimals.abs_diff(info.internal_decimals) as u32) <= MAX_DECIMALS_DIFF,
				Error::<T>::DecimalsRangeExceeded
			);

			ExternalAssets::<T>::insert(
				&internal_asset,
				&external_asset,
				ExternalAssetInfo {
					status: CircuitBreakerLevel::AllEnabled,
					decimals: asset_decimals,
				},
			);
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
