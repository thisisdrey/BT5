Based on my investigation, I found a concrete local analog in `pallet-psm`'s `mint` function, which mirrors the ERC5095 bug's core flaw: computing/enforcing a bound (or in this case, a caller-authorized amount) against the wrong quantity due to a lossy round-trip conversion, causing the user to actually pay more than the amount they authorized.

### Title
PSM `mint` transfers `effective_external` (a round-tripped, potentially inflated value) instead of the user-authorized `external_amount` - (File: substrate/frame/psm/src/lib.rs)

### Summary
`pallet_psm::Pallet::mint` [1](#0-0)  takes the caller-specified `external_amount`, converts it to an `internal_equivalent` via `external_to_internal`, and then re-derives `effective_external` via `internal_to_external(internal_equivalent, ...)` — a second, independent conversion, not simply `external_amount` itself. The actual `T::Fungibles::transfer` charges the user `effective_external`, not the amount they specified [2](#0-1) . Just as ERC5095 `mint` used a slippage bound computed from the wrong unit (`assets` instead of `shares`), this PSM `mint` computes the "amount actually charged" via a round-trip decimal conversion rather than directly honoring the value the caller authorized, so the value debited from the user's account can diverge from `external_amount`.

### Finding Description
The round-trip is: `external_amount → internal_equivalent (external_to_internal) → effective_external (internal_to_external)`. These are two independent fixed-point decimal-scaling conversions (external decimals ↔ internal decimals), each of which can round in either direction depending on implementation (the doc comments state truncation dust is intentionally left with the caller for symmetry with `redeem`, implying `internal_to_external` and `external_to_internal` are not guaranteed to be perfect inverses under rounding). If the internal implementation of `internal_to_external` rounds up (ceiling) relative to `external_to_internal`'s rounding down, or if a decimals-difference scale factor is applied asymmetrically, `effective_external` can exceed the user-provided `external_amount`.

Because the pallet's `T::Fungibles::transfer` in the mint call uses `effective_external` — not `external_amount` — as the debited quantity, this is directly analogous to the ERC5095 bug: the function accepts a caller-specified target parameter (`external_amount`, analogous to `s`/shares in ERC5095) but the value that actually moves funds is derived from an intermediate, converted quantity (`effective_external`, analogous to `assets`) which can silently diverge from what the caller intended to pay. There is no `ensure!(effective_external <= external_amount)` guard anywhere in `mint` to cap the amount actually charged to what the caller authorized.

### Impact Explanation
If `effective_external > external_amount` for any combination of `ext_decimals`/`internal_decimals` and specific input value (which is plausible given the decimals-diff scaling described in the PSM decimals-precision changes) [3](#0-2) , a caller who intends to spend only `external_amount` of the external asset is instead debited more, resulting in direct fund loss to the user on every mint call that hits an unfavorable rounding boundary. This falls squarely under "theft or unbacked mint or unlock" / value-conservation violations in the accepted impact list, since accounting state (the amount actually transferred from the user) does not match the amount the extrinsic parameter authorized.

### Likelihood Explanation
Likelihood depends on the exact rounding behavior of `external_to_internal`/`internal_to_external`, which I was not able to fully verify — the final grep for their bodies did not return results before the iteration limit was reached. This is the key piece of missing evidence: without seeing the exact rounding direction (floor vs ceil) in both conversion functions, I cannot conclusively prove `effective_external > external_amount` is reachable rather than merely `effective_external <= external_amount` (safe, dust-losing) in all cases. The doc comment on `redeem` explicitly says dust stays with the caller "symmetric with `mint`, which takes only the round-tripped share of the external amount," which suggests the pallet author intended `effective_external <= external_amount` always — but this needs to be confirmed against the actual conversion helper implementation before treating this as a proven live vulnerability.

### Recommendation
Add an explicit invariant check `ensure!(effective_external <= external_amount, Error::<T>::Unexpected)` before performing the transfer in `mint`, and add a corresponding defensive/property test asserting `internal_to_external(external_to_internal(x)) <= x` for all decimal-diff combinations within `MAX_DECIMALS_DIFF`. Alternatively, transfer `external_amount` directly (matching what the user authorized) rather than the round-tripped `effective_external`, and reconcile any residual difference through the fee/dust accounting instead of the transfer amount.

### Proof of Concept
Not fully constructible without confirmed source of `external_to_internal`/`internal_to_external` — this requires reading `substrate/frame/psm/src/lib.rs` (the conversion helper functions, likely further down in the file past what I was able to retrieve) to construct concrete `(ext_decimals, internal_decimals, external_amount)` triples where `effective_external > external_amount`. I recommend a Devin session inspect these two functions directly and write a unit test in `substrate/frame/psm/src/tests.rs` iterating decimal-diff combinations to search for a counterexample before treating this as confirmed.

Given the residual uncertainty on the rounding direction, I present this as the strongest local analog found, but flag it as **not fully proven** — the missing piece is the concrete body of `external_to_internal`/`internal_to_external`, which the index/tool budget did not let me retrieve before this final iteration. If you want, a follow-up session could pull those functions directly from `substrate/frame/psm/src/lib.rs` to close out proof or disprove this line of attack.

### Citations

**File:** substrate/frame/psm/src/lib.rs (L702-730)
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
```

**File:** substrate/frame/psm/src/lib.rs (L743-750)
```rust
			let psm_account = Self::psm_account(&internal_asset);
			T::Fungibles::transfer(
				external_asset.clone(),
				&who,
				&psm_account,
				effective_external,
				Preservation::Expendable,
			)?;
```
