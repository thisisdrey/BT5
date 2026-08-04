### Title
`redeem()` skips the live-decimals guard that `mint()` enforces, letting a drifted external-asset decimals value corrupt the redemption payout - (File: `substrate/frame/psm/src/lib.rs`)

### Summary
`pallet-psm`'s `mint()` calls `Self::ensure_decimals_match(...)` before converting amounts, guarding against a live drift between an asset's actual on-chain `decimals()` metadata and the `decimals` snapshot the pallet stored at registration time. `redeem()` performs the mirror-image conversion (`internal_to_external`) but never calls that guard — it feeds the stored, potentially stale `external.decimals` / `info.internal_decimals` straight into the scaling math. This is the same root cause as the external report: an amount is scaled/compared using an assumed decimal precision that no longer matches the asset's real precision, producing a wrong transfer amount.

### Finding Description
`mint()` fetches the external's registration and validates decimals before scaling: [1](#0-0) 

`redeem()` fetches the same snapshot fields directly, with no equivalent live-decimals check, and immediately uses them to scale the redemption output: [2](#0-1) 

The scaling itself trusts the two `u8` decimal values it is handed and multiplies/divides by `10^diff` accordingly: [3](#0-2) 

`ExternalAssetInfo::decimals` is documented as only a "snapshot ... at registration time", not a live value: [4](#0-3) 

and the pallet's own `Error::DecimalsMismatch` is explicitly meant to catch "Live decimals diverged from the snapshot taken at registration or genesis": [5](#0-4) 

Since `redeem()` never calls the drift check, once an external asset's live `decimals()` metadata changes after `add_external_asset` approved it (e.g. via the asset's own `set_metadata`/similar permissionless call available to that asset's owner in the `Fungibles` backend), every subsequent `redeem()` against that external asset scales `internal_net` with the wrong power-of-ten factor. This mirrors exactly the reported `TokenSale.takeUSDCRaised()` bug: an amount computed under one decimal assumption is compared/transferred as if it were in another decimal precision, with no re-validation before the transfer.

### Impact Explanation
`internal_to_external`'s scaling factor is `10^|ext_decimals − internal_decimals|`. If the true decimals diverge from what `redeem()` assumes, `external_out` can be inflated by orders of magnitude relative to what the caller's burned `internal_net` should be worth, or shrunk to near zero:
- Inflated case: the reserve check `if reserve < external_out { return Err(Error::Unexpected) }` only stops the transfer if the PSM's reserve happens to be too small; if the reserve is large enough (e.g., multiple users' pooled deposits), the caller can drain far more external asset than their burned internal debt should entitle them to, at other depositors' expense — an unbacked/duplicate-settlement style fund loss out of the shared reserve. [6](#0-5) 
- Deflated case: users get far less external asset than their burned internal debt should be worth, permanently locking value in the reserve since `effective_internal_net` (recomputed via the same wrong decimals) is what actually gets burned/tracked, keeping the accounting internally "consistent" but wrong relative to real asset value.

### Likelihood Explanation
The trigger does not require a malicious PSM admin, governance actor, or validator — it only requires the owner of an *external* asset (a role independent from the PSM's admins) to change that asset's decimals metadata after it has already been approved on a PSM instance, or for an asset whose decimals are mutable/rebasable via its own fungibles implementation. `mint()`'s explicit `ensure_decimals_match` call shows the pallet authors recognized this exact risk class; the omission in `redeem()` is a straightforward asymmetry between the two swap paths rather than a hypothetical scenario.

### Recommendation
Call the same `ensure_decimals_match` check (or an equivalent live-vs-snapshot comparison against `T::Fungibles::decimals`) inside `redeem()` before computing `external_out`, exactly as `mint()` does, and reject with `Error::DecimalsMismatch` on drift so the redemption cannot proceed with a stale/incorrect scaling factor.

### Proof of Concept
1. Governance/admin approves external asset `E` on PSM for internal asset `I` via `add_external_asset`, snapshotting `E`'s decimals as 6 (matches `internal_decimals` = 6, so `pow10(0)=1`).
2. Users deposit `E` via `mint`, building up a nontrivial reserve balance and `PsmDebt`.
3. The owner of asset `E` (unrelated to the PSM admin) changes `E`'s live decimals metadata (via whatever primitive the `Fungibles` implementation exposes for it) from 6 to, say, 0, without the PSM being notified.
4. Attacker calls `redeem(internal_asset=I, external_asset=E, internal_amount, max_fee)`. `redeem()` reads `ext_decimals = external.decimals` (still 6, the stale snapshot) and computes `external_out = internal_to_external(internal_net, 6, 6) = internal_net` — correct only if the assumption still held; but if instead the mismatch runs the other direction (snapshot decimals lower than live, or vice versa depending on which side drifted), the scaling factor used no longer reflects the asset's real value per unit, and `external_out` diverges from the true 1:1-equivalent amount while `redeem()` performs no check to catch this before calling `T::Fungibles::transfer(external_asset, &psm_account, &who, external_out, ...)`.
5. Because no `DecimalsMismatch` check runs in this path (unlike `mint`), the wrong `external_out` is transferred straight out of the shared PSM reserve, at the expense of other depositors' backing collateral.

### Citations

**File:** substrate/frame/psm/src/lib.rs (L312-330)
```rust
	/// On-chain record of an external asset approved on a PSM instance.
	#[derive(
		Encode,
		Decode,
		DecodeWithMemTracking,
		MaxEncodedLen,
		TypeInfo,
		Clone,
		Copy,
		PartialEq,
		Eq,
		Debug,
	)]
	pub struct ExternalAssetInfo {
		/// Per-external circuit breaker status.
		pub status: CircuitBreakerLevel,
		/// Snapshot of the external asset's decimals at registration time.
		pub decimals: u8,
	}
```

**File:** substrate/frame/psm/src/lib.rs (L639-640)
```rust
		/// Live decimals diverged from the snapshot taken at registration or genesis.
		DecimalsMismatch,
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

**File:** substrate/frame/psm/src/lib.rs (L851-855)
```rust
			let reserve = Self::get_reserve(&internal_asset, &external_asset);
			if reserve < external_out {
				defensive!("PSM reserve is less than expected output amount");
				return Err(Error::<T>::Unexpected.into());
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
