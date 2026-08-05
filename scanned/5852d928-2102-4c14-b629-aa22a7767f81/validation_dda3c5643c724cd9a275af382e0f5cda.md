### Title
`redeem` skips the decimals-drift guard that `mint` enforces, allowing stale-decimal conversion to mis-price PSM swaps - (File: `substrate/frame/psm/src/lib.rs`)

### Summary
The external report's core invariant is "amount math must be computed with the token's actual decimal precision, or fee/transfer amounts become wrong." `pallet-psm` (`substrate/frame/psm/src/lib.rs`) implements exactly this concern for its stablecoin swap mechanism, and does it correctly in `mint` — but `redeem` omits the same protection, creating a decimals-precision analog of the original bug class.

### Finding Description
`mint` fetches the external asset's decimal count through a dedicated drift guard: [1](#0-0) 
This helper (`ensure_decimals_match`) is documented as returning `Error::DecimalsMismatch` "If live decimals diverged from the snapshot taken at registration," halting the asset until governance intervenes, per the PR description: [2](#0-1) 

`redeem`, however, reads the decimals directly from stored state without going through that guard: [3](#0-2) 
```
let external = ExternalAssets::<T>::get(&internal_asset, &external_asset)
    .ok_or(Error::<T>::UnsupportedAsset)?;
ensure!(external.status.allows_redemption(), Error::<T>::AllSwapsStopped);

let ext_decimals = external.decimals;
let internal_decimals = info.internal_decimals;
```
No call to `ensure_decimals_match` exists anywhere in `redeem` — the function proceeds straight to the fee and conversion math (`external_to_internal` / `internal_to_external`) using whatever decimals value is cached in the `ExternalAssets` snapshot, even if the live fungibles metadata has since diverged: [4](#0-3) 

Because `ext_decimals`/`internal_decimals` directly parameterize the `10^diff` scaling factor used in `internal_to_external`/`external_to_internal`: [5](#0-4) [6](#0-5) 
a stale decimals snapshot causes the scaling factor to diverge from the real ratio between the two assets' actual smallest units. Since `redeem` moves real balances (`T::Fungibles::burn_from` on the internal asset and `T::Fungibles::transfer` of `external_out` out of the PSM reserve) computed from this mis-scaled arithmetic, the amount of external asset paid out per unit of internal asset burned no longer reflects the true 1:1 economic peg the PSM is meant to preserve.

### Impact Explanation
This breaks the "Balances, assets… must conserve value and settle exactly once to the rightful beneficiary and amount" invariant required by the impact gate. If an external asset's on-chain decimals metadata can change after PSM registration (e.g., the asset owner updates metadata, or the asset is re-created/migrated with different decimals — a realistic path for permissionless `pallet-assets` instances), `redeem` will keep using the outdated scaling factor while `mint` would correctly halt with `DecimalsMismatch`. An attacker who can influence or benefit from the decimals drift can redeem internal stablecoin for a disproportionate amount of external reserve (or vice versa drain more reserve than the burned internal amount justifies), silently draining the PSM's `external_asset` reserve — a form of unbacked value extraction from a public, unprivileged entry point.

### Likelihood Explanation
`redeem` is a public, unauthenticated (any signed account) extrinsic, so no validator/relayer/admin collusion is required — only a decimals drift condition on the external asset, which the pallet's own design (`AssetDecimals` snapshot, `DecimalsMismatch` error, `PopulateDecimals` migration) shows is an anticipated, non-hypothetical class of event this pallet was built to defend against. The asymmetry — the guard present in `mint` but absent in `redeem` — is a straightforward code-level omission rather than a speculative external assumption.

### Recommendation
Add the same `ensure_decimals_match` (or equivalent) call at the top of `redeem`, mirroring `mint`'s guard, so that any live-metadata divergence from the registered `AssetDecimals`/`StableDecimals` snapshot halts redemption with `Error::DecimalsMismatch` instead of proceeding with stale scaling factors.

### Proof of Concept
1. Register an external asset in a PSM instance with decimals `D1` (captured in the `ExternalAssets` / `AssetDecimals` snapshot).
2. Update the live decimals metadata of that asset (e.g., via `pallet-assets::set_metadata` if the asset owner is unprivileged/uncontrolled by PSM governance) to `D2 ≠ D1`.
3. Call `mint` — it fails with `Error::DecimalsMismatch` because `ensure_decimals_match` detects the drift.
4. Call `redeem` with the same asset pair — it succeeds because `redeem` never calls `ensure_decimals_match`, using the stale `D1` scaling factor in `internal_to_external`/`external_to_internal` against the real balance amounts, yielding an `external_out` that no longer matches the intended 1:1 peg and can be used to extract disproportionate reserve value.

### Citations

**File:** substrate/frame/psm/src/lib.rs (L716-717)
```rust
			let (ext_decimals, internal_decimals) =
				Self::ensure_decimals_match(&info, &internal_asset, &external_asset, &external)?;
```

**File:** substrate/frame/psm/src/lib.rs (L819-826)
```rust
			let info = Psm::<T>::get(&internal_asset).ok_or(Error::<T>::PsmNotFound)?;

			let external = ExternalAssets::<T>::get(&internal_asset, &external_asset)
				.ok_or(Error::<T>::UnsupportedAsset)?;
			ensure!(external.status.allows_redemption(), Error::<T>::AllSwapsStopped);

			let ext_decimals = external.decimals;
			let internal_decimals = info.internal_decimals;
```

**File:** substrate/frame/psm/src/lib.rs (L828-846)
```rust
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

**File:** prdoc/stable2606/pr_11819.prdoc (L18-21)
```text
      checks are meaningful across mixed-decimal assets.
    - Runtime drift guard: `mint`/`redeem` return `DecimalsMismatch` if live
      metadata diverges from the registration snapshot; that asset halts until
      governance intervenes.
```
