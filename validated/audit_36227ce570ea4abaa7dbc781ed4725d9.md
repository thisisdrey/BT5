### Title
Aggregate `PsmDebt` can be driven below `min_swap_amount` by normal fee/decimal rounding, permanently locking the corresponding external reserve — (File: `substrate/frame/psm/src/lib.rs`)

### Summary
`pallet-psm`'s `redeem()` reduces the tracked debt for an `(internal_asset, external_asset)` pair by `effective_internal_net`, a value that is *rounded down* after the redemption fee is applied and after decimal round-tripping. `redeem()` additionally enforces a fixed floor, `min_swap_amount`, on the caller-supplied `internal_amount`, independent of how much aggregate debt is actually outstanding. When the last redemption(s) on a given pair leave `PsmDebt` at a value smaller than `min_swap_amount`, no future call — from any account — can ever clear it, because any request that satisfies the `min_swap_amount` floor will necessarily ask for more than the remaining debt and trip `InsufficientReserve`, while any request small enough to fit under the remaining debt fails `BelowMinimumSwap`. The external reserve backing that dust debt is then permanently stranded in the PSM reserve account.

### Finding Description
`redeem()` computes:
```
let fee = fee_rate.mul_ceil(internal_amount);
let internal_net = internal_amount.saturating_sub(fee);
let external_out = Self::internal_to_external(internal_net, ext_decimals, internal_decimals)?;
let effective_internal_net = Self::external_to_internal(external_out, ext_decimals, internal_decimals)?;
...
ensure!(current_debt >= effective_internal_net, Error::<T>::InsufficientReserve);
...
PsmDebt::<T>::mutate(&internal_asset, &external_asset, |debt| {
    *debt = debt.saturating_sub(effective_internal_net);
});
``` [1](#0-0) [2](#0-1) 

`effective_internal_net` is a floor-rounded round-trip of the post-fee amount (`internal_to_external` then `external_to_internal`), so it is always `<= internal_amount`, and the call is still gated by:
```
ensure!(internal_amount >= info.min_swap_amount, Error::<T>::BelowMinimumSwap);
``` [3](#0-2) 

This is the same underlying broken invariant as the Union Finance report: a fee-adjusted quantity is floor-rounded and used to update an aggregate accounting value (`PsmDebt`, analogous to `updateLocked`'s locked amount), while a separate exact-amount threshold check (`min_swap_amount`, analogous to the principal/borrowed check) is applied against the caller's *requested* amount rather than the *actual remaining debt*. Once enough mint/redeem cycles (each contributing fee-driven and decimal-conversion rounding) leave `PsmDebt` at a nonzero value below `min_swap_amount`:
- Any `redeem` call with `internal_amount < min_swap_amount` fails `BelowMinimumSwap`.
- Any `redeem` call with `internal_amount >= min_swap_amount` produces `effective_internal_net > current_debt`, failing `InsufficientReserve`.

No existing guard reconciles these two checks against each other, so the dust debt (and its backing reserve, held in `Self::psm_account(&internal_asset)`) becomes permanently unreachable by any public call, exactly mirroring how Union Finance's rounded-down `updateLocked` value left `locked < principal` permanently unresolvable via the normal write-off path.

### Impact Explanation
The external asset amount corresponding to the stuck `PsmDebt` dust is permanently locked in the PSM reserve account — no user, and no unprivileged public entrypoint, can ever extract it via `redeem`. This matches the required impact category "permanent user-fund or bridge-state lock." It occurs purely through routine, permissionless use of the public `mint`/`redeem` extrinsics (no admin/governance action, no malicious peer/relayer/validator assumption needed).

### Likelihood Explanation
This is reachable by ordinary users performing normal mint/redeem cycles on any PSM pair configured with mixed decimals and a non-zero redemption fee (both are supported, documented configurations — see the `decimal_scaling` tests and `mul_ceil` fee handling). The narrower the gap between the outstanding debt and `min_swap_amount` near depletion of a pair's reserve, the more likely this state is reached; it requires no adversarial coordination, just enough sequential redemptions (which the tests already show approach exactly-zero debt in the "cycle" test, indicating this boundary is actively exercised in practice).

### Recommendation
Do not gate `redeem` solely on the fixed `min_swap_amount` against the requested `internal_amount`. Additionally allow (or force) full closure of the remaining debt when `current_debt < min_swap_amount`, e.g., by permitting `internal_amount` up to `current_debt` to bypass `min_swap_amount` when it would fully clear the pair's debt, or by rounding `effective_internal_net` such that a final redemption can be sized to exactly drain any sub-threshold remainder.

### Proof of Concept
1. Register an internal asset (6 decimals) with an external asset of higher precision (e.g. 18 decimals) and set a non-zero `min_swap_amount` and non-zero `RedemptionFee`.
2. Multiple users `mint` and `redeem` normally over time; each `redeem` reduces `PsmDebt` by `effective_internal_net` (floor-rounded post-fee, post-decimal round-trip).
3. Arrange (through ordinary usage, as already demonstrated by the repo's own `decimal_scaling`/redeem tests showing dust and near-zero-debt behavior) for the final redemption(s) to leave `PsmDebt::<T>::get(internal_asset, external_asset)` at a small positive value `d < min_swap_amount`.
4. Attempt `redeem(internal_asset, external_asset, min_swap_amount, max_fee)`: fails with `InsufficientReserve` because `effective_internal_net > d`.
5. Attempt `redeem(internal_asset, external_asset, d, max_fee)` (or any amount `< min_swap_amount`): fails with `BelowMinimumSwap`.
6. `PsmDebt` remains stuck at `d` forever, and the corresponding external reserve remains permanently locked in `Self::psm_account(&internal_asset)`.

### Citations

**File:** substrate/frame/psm/src/lib.rs (L828-849)
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

**File:** substrate/frame/psm/src/lib.rs (L889-891)
```rust
			PsmDebt::<T>::mutate(&internal_asset, &external_asset, |debt| {
				*debt = debt.saturating_sub(effective_internal_net);
			});
```
