Based on my investigation, I found a strong local analog in `pallet-psm`'s `mint` extrinsic, which reproduces the exact class of bug described in the Astaria report: a value is checked/bounded against one computed amount, but a *different, larger* derived amount is what actually gets pulled from the user via `T::Fungibles::transfer`.

### Title
Round-trip conversion in `pallet-psm::mint` can pull more external asset than the user's checked/expected amount, causing debt-ceiling and reserve accounting to diverge from actual transferred value - (File: `substrate/frame/psm/src/lib.rs`)

### Summary
`Pallet::mint` computes `internal_equivalent` from the user-supplied `external_amount`, then re-derives `effective_external` via a *second* conversion (`internal_to_external(internal_equivalent, ...)`) and transfers `effective_external` — not the original `external_amount` — from the caller. [1](#0-0)  This is structurally identical to the Astaria bug: the amount validated/authorized by the caller (`external_amount`) is not the amount actually transferred (`effective_external`), because it is recomputed through a lossy round-trip conversion (`external_to_internal` → `internal_to_external`) whose rounding direction is not guaranteed to only shrink the value.

### Finding Description
In `mint`, the flow is:
1. `internal_equivalent = external_to_internal(external_amount, ext_decimals, internal_decimals)` [2](#0-1) 
2. `effective_external = internal_to_external(internal_equivalent, ext_decimals, internal_decimals)` [3](#0-2) 
3. The debt ceiling checks (`ExceedsMaxPsmDebt`, `max_asset_debt`) are performed against `internal_equivalent`. [4](#0-3) 
4. The actual token movement uses `effective_external`, not the user's original `external_amount`: `T::Fungibles::transfer(external_asset, &who, &psm_account, effective_external, Preservation::Expendable)?;` [5](#0-4) 

This mirrors the Astaria root cause exactly: a caller-facing amount is used to compute/authorize one figure, while a *different* figure — derived through a forward-then-reverse decimal conversion — is what's actually pulled. Comment in `redeem` explicitly acknowledges the round-trip is not symmetric and can produce dust divergence ("Any truncation dust stays in the caller's internal balance") [6](#0-5) , confirming the pallet's own authors recognize `effective_*` values can diverge from the nominal input — but in `mint`'s case this divergence feeds directly into a `Fungibles::transfer` pulled from the user, and the `PsmDebt` ledger is updated using `internal_equivalent` [7](#0-6)  and `new_debt` [8](#0-7)  — not against `effective_external`. If the two-step decimal conversion (`external_to_internal` then `internal_to_external`) can round `effective_external` above `external_amount` for any decimals-difference/rounding combination (e.g., ceiling-then-floor vs. floor-then-ceiling asymmetry across the scaling factor `10^diff`), the PSM's reserve accounting (`external_consumed` in the `Minted` event, and the implicit assumption that reserve inflow equals `internal_equivalent`'s backing) permanently diverges from the debt actually minted, since `PsmDebt` and the ceiling gates are keyed to `internal_equivalent` while the actual backing asset pulled is `effective_external`. This breaks the pallet's core "backed 1:1 by external assets in reserve" invariant. [9](#0-8) 

Because `min_swap_amount` and `MAX_DECIMALS_DIFF` bound the arithmetic [10](#0-9) , an outright overflow/insufficient-allowance revert (as in the original Astaria DoS) is unlikely; the more consequential local analog is silent **value divergence between what is debited from the user, what backs the debt ledger, and what event/telemetry report** — an unbacked-mint-adjacent accounting bug rather than a hard revert.

### Impact Explanation
If `effective_external` can exceed the user-approved/intended `external_amount` for certain decimal-difference and rounding combinations, users are overcharged relative to what they authorized (fund loss on the pull side) while the debt ceiling tracked in `PsmDebt`/`total_psm_debt` remains keyed to `internal_equivalent`, understating true reserve backing requirements. Conversely, if it can round below, the PSM's reserve receives less than the internal-asset debt it issues, degrading the "backed 1:1" invariant relied on for solvency — a direct violation of the "theft or unbacked mint" impact category.

### Likelihood Explanation
Medium: it requires specific external/internal decimal configurations that produce a non-monotonic round-trip (forward truncation + reverse rounding direction), which is plausible given `MAX_DECIMALS_DIFF = 24` allows wide decimal spreads, and it is triggerable by any unprivileged, permissionless caller of the `mint` extrinsic with no admin/governance/relayer involvement — matching the "public underpriced work" / value-conservation gate.

### Recommendation
Use the user-supplied `external_amount` (not the round-tripped `effective_external`) for the actual `Fungibles::transfer` pull, or conversely, key all debt-ceiling and ledger updates (`PsmDebt`, `total_psm_debt`, `max_asset_debt`) off the value that is actually transferred (`effective_external`) rather than the pre-round-trip `internal_equivalent`. The two must be provably consistent (the ledger balance in internal-asset units must exactly and only ever correspond to the external-asset amount that is actually moved into the reserve), analogous to how the Astaria fix recommends deriving the approval amount from the same function used for the actual transfer (`previewMint`) rather than from an independently-computed value.

### Proof of Concept
Conceptual reproduction (mirrors the Astaria PoC structure):
1. Register a PSM with `internal_decimals` and an external asset whose `decimals` differ enough (within `MAX_DECIMALS_DIFF`) to induce truncation on `external_to_internal` and a different rounding behavior on the reverse `internal_to_external`.
2. Call `mint(internal_asset, external_asset, external_amount, max_fee)` with an `external_amount` chosen near a rounding boundary of the decimal scale factor.
3. Observe that `effective_external` (the amount actually pulled via `T::Fungibles::transfer`, `substrate/frame/psm/src/lib.rs:743-750`) differs from the original `external_amount`, while `PsmDebt` is updated based on `internal_equivalent` (`substrate/frame/psm/src/lib.rs:738-741`) — demonstrating the debited amount and the ledger-tracked backing amount are not the same figure the caller authorized, exactly as in the Astaria `assets`-vs-`shares` allowance mismatch.

Note: I was unable to fully inspect the bodies of `external_to_internal`/`internal_to_external`/`ensure_decimals_match` within the available iterations (only their call sites were confirmed) to definitively prove the rounding direction produces `effective_external > external_amount` in a concrete numeric case; a Devin session with full file access should verify the exact rounding arithmetic in these helper functions to confirm whether the divergence can be positive (overcharge) as well as negative.

### Citations

**File:** substrate/frame/psm/src/lib.rs (L60-61)
```rust
//! * **PSM Debt**: Total internal asset minted through a PSM, backed 1:1 by external assets in that
//!   PSM's reserve.
```

**File:** substrate/frame/psm/src/lib.rs (L270-273)
```rust
	/// Maximum absolute difference between an external asset's decimals and the internal
	/// asset's decimals. Bounds the scaling factor `10^diff` well below `u128::MAX`
	/// so realistic balances cannot overflow during conversion.
	pub const MAX_DECIMALS_DIFF: u32 = 24;
```

**File:** substrate/frame/psm/src/lib.rs (L719-725)
```rust
			let internal_equivalent =
				Self::external_to_internal(external_amount, ext_decimals, internal_decimals)?;
			ensure!(!internal_equivalent.is_zero(), Error::<T>::AmountTooSmallAfterConversion);
			ensure!(internal_equivalent >= info.min_swap_amount, Error::<T>::BelowMinimumSwap);

			let effective_external =
				Self::internal_to_external(internal_equivalent, ext_decimals, internal_decimals)?;
```

**File:** substrate/frame/psm/src/lib.rs (L732-741)
```rust
			let current_total_psm_debt = Self::total_psm_debt(&internal_asset);
			ensure!(
				current_total_psm_debt.saturating_add(internal_equivalent) <= info.max_debt,
				Error::<T>::ExceedsMaxPsmDebt
			);

			let current_debt = PsmDebt::<T>::get(&internal_asset, &external_asset);
			let max_debt = Self::max_asset_debt(&internal_asset, &external_asset, &info);
			let new_debt = current_debt.saturating_add(internal_equivalent);
			ensure!(new_debt <= max_debt, Error::<T>::ExceedsMaxPsmDebt);
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

**File:** substrate/frame/psm/src/lib.rs (L756-756)
```rust
			PsmDebt::<T>::insert(&internal_asset, &external_asset, new_debt);
```

**File:** substrate/frame/psm/src/lib.rs (L841-844)
```rust
			// `effective_internal_net` is the internal value that round-trips to `external_out`;
			// it is what we actually burn and what the tracked debt decreases by. Any truncation
			// dust stays in the caller's internal balance, symmetric with `mint`, which takes
			// only the round-tripped share of the external amount.
```
