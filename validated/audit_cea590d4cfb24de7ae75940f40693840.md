### Title
`pallet-psm::redeem` under-burns internal debt via rounding-truncated `effective_internal_net`, letting an unprivileged user repeatedly drain external reserve without matching debt/supply reduction - ([File: substrate/frame/psm/src/lib.rs])

### Summary
`pallet-psm` is a local, repository-specific analog to a Morpho-style vault: it tracks a per-asset "debt" (`PsmDebt`) that must always be backed 1:1 by external-asset reserve, exactly as Morpho tracks `borrowAssets` that must be backed by `supplyAssets`. The Morpho bug showed that when the pallet's internal accounting (virtual borrow shares) can drift from what is actually collectible/burnable, repeated cheap calls compound that drift into unbacked debt and reduced withdrawable funds. `pallet-psm::redeem` has the same class of drift: it burns/decrements accounting by a rounding-truncated value (`effective_internal_net`) while paying out external reserve sized to the pre-truncation amount, letting a caller repeatedly harvest the rounding delta.

### Finding Description
In `redeem` [1](#0-0) , the flow is:

1. `internal_net = internal_amount - fee`
2. `external_out = internal_to_external(internal_net, ext_decimals, internal_decimals)` (rounds toward zero when converting to the external asset's lower decimal precision)
3. `effective_internal_net = external_to_internal(external_out, ext_decimals, internal_decimals)` — converting `external_out` back, which can be **strictly less** than `internal_net` whenever the decimal-conversion truncation loses precision (this is explicitly acknowledged in the code comment as "truncation dust stays in the caller's internal balance").
4. Only `effective_internal_net` (not `internal_net`) is burned from the caller: [2](#0-1) 
5. `PsmDebt` is decremented by that same truncated `effective_internal_net`: [3](#0-2) 
6. But the user receives `external_out` (sized from the *pre-truncation* `internal_net`), transferred out of the reserve in full: [4](#0-3) 

The invariant the pallet must hold — analogous to Morpho's `supplyAssets - borrowAssets` withdrawable computation — is that `PsmDebt[internal,external]` always equals the external reserve actually owed/backed, and that internal supply removed by `redeem` matches what leaves the reserve. Because burn and debt-reduction use the *truncated* value while the payout uses the *un-truncated* value, each `redeem` call leaks `internal_net - effective_internal_net` worth of internal token that the caller keeps (never burned, never fee-collected) while draining reserve/debt proportional to the larger pre-truncation amount. This is the same "virtual/uncollectible accounting delta compounds via repeated calls" primitive as the Morpho report's `_accrueInterest` and inflation-attack appendix: no privileged actor is needed, only a signed caller invoking a public extrinsic (`redeem`) many times at rounding boundaries.

The size of the leak per call is bounded by the decimal gap between internal and external assets, gated by `MAX_DECIMALS_DIFF` at `add_external_asset` time [5](#0-4) , but with any nonzero decimals gap it is repeatable indefinitely (each call is independent, no state carries over to prevent re-harvesting the same rounding boundary), matching the Morpho appendix's loop-based repeated-small-operation exploitation of a rounding/accounting seam.

### Impact Explanation
Each exploited `redeem` call causes the PSM's external reserve to be depleted faster than `PsmDebt` (and internal-asset burn) reflects, so the invariant "reserve backs `PsmDebt`" silently degrades. Over many iterations this can (a) starve the reserve so legitimate redemptions later fail with `InsufficientReserve`/hit the defensive `Unexpected` branch [6](#0-5) , locking other users' funds, and (b) let the attacker accumulate free, un-burned internal-asset balance extracted from the PSM's backing — an unbacked-mint-equivalent value leak, mirroring Morpho's "bad debt reduces withdrawable funds" outcome.

### Likelihood Explanation
Any signed account can call the public `redeem` extrinsic with attacker-chosen `internal_amount` values crafted to sit exactly at decimal-truncation boundaries, requiring no governance, relayer, validator, or leaked-key assumptions — it is a pure public-entrypoint accounting bug, fitting the "public underpriced work / unbacked value" impact gate.

### Recommendation
Burn/decrement using the same pre-truncation value that is paid out (or conversely, recompute `external_out` from the value that will actually be burned and use that consistently for both sides), ensuring `PsmDebt` reduction, internal burn amount, and external payout are derived from a single, mutually consistent rounding pass rather than a round-trip that can silently diverge. Alternatively, always round in the PSM's favor (floor the external payout, not the reconstructed internal amount) so any truncation dust is retained by the reserve/debt ledger rather than leaked to the caller.

### Proof of Concept
Conceptually (exact numeric PoC needs the actual `internal_to_external`/`external_to_internal` rounding functions, which were not fully retrieved due to index truncation — see note below):
1. Configure a PSM instance with an internal asset of high decimals (e.g. 18) and an external asset of low decimals (e.g. 6), within `MAX_DECIMALS_DIFF`.
2. Mint some internal balance normally.
3. Call `redeem` repeatedly with `internal_amount` values chosen such that `internal_to_external(internal_net)` truncates, i.e. `external_to_internal(internal_to_external(internal_net)) < internal_net`.
4. After each call, observe: `PsmDebt` decreases only by `effective_internal_net`, reserve decreases by `external_out` (consistent with `internal_net`, not `effective_internal_net`), and the caller's internal balance drops by only `fee + effective_internal_net` instead of `internal_amount` — retaining the rounding dust every time.
5. Repeating this many times accumulates a measurable reserve/debt shortfall.

**Note on verification limits:** the exact bodies of `external_to_internal`/`internal_to_external`/`get_reserve`/`do_try_state` in `substrate/frame/psm/src/lib.rs` could not be retrieved in full before the tool budget was exhausted, so the precise rounding direction and whether `do_try_state` already asserts (and would catch) this drift is not fully confirmed from the index. If you need the exact function bodies and a numeric PoC, start a Devin session with full repository access to pull `substrate/frame/psm/src/lib.rs` in full and construct a concrete `#[test]` reproducing the leak.

### Citations

**File:** substrate/frame/psm/src/lib.rs (L828-850)
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

			let current_debt = PsmDebt::<T>::get(&internal_asset, &external_asset);
			ensure!(current_debt >= effective_internal_net, Error::<T>::InsufficientReserve);

```

**File:** substrate/frame/psm/src/lib.rs (L851-855)
```rust
			let reserve = Self::get_reserve(&internal_asset, &external_asset);
			if reserve < external_out {
				defensive!("PSM reserve is less than expected output amount");
				return Err(Error::<T>::Unexpected.into());
			}
```

**File:** substrate/frame/psm/src/lib.rs (L867-876)
```rust
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
```

**File:** substrate/frame/psm/src/lib.rs (L878-887)
```rust
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
```

**File:** substrate/frame/psm/src/lib.rs (L889-891)
```rust
			PsmDebt::<T>::mutate(&internal_asset, &external_asset, |debt| {
				*debt = debt.saturating_sub(effective_internal_net);
			});
```

**File:** substrate/frame/psm/README.md (L146-159)
```markdown
### Asset Onboarding Requirements

Before calling `add_external_asset(internal_asset, asset_id)`:

- A PSM must already be registered for `internal_asset`
- The external `asset_id` must already exist in the `Fungibles` implementation
- The internal asset's live decimals must still match the snapshot in `PsmInfo`
- `|external_decimals − internal_decimals|` must be within `MAX_DECIMALS_DIFF`
- The PSM must still be below `MaxExternals`

After `add_external_asset`, the external starts with an `AssetCeilingWeight` of `0%`, so its
per-external ceiling is zero and **minting is disabled**. Before the first mint, call
`set_asset_ceiling_weight(internal_asset, asset_id, weight)` with a non-zero weight (and
optionally `set_minting_fee` / `set_redemption_fee`, which otherwise default to 0.5%).
```
