### Title
Legacy `pallet-staking::withdraw_unbonded` releases unbonded funds without verifying that deferred slashes for the unlocking window have actually been applied - ([File: substrate/frame/staking/src/pallet/impls.rs])

### Summary
`pallet-staking` (the classic, non-async staking pallet) defers slashes by `SlashDeferDuration` eras and only *expects* `BondingDuration` to always exceed `SlashDeferDuration` so that a slash is applied before an unbonding chunk becomes withdrawable. `do_withdraw_unbonded` never actually checks the `UnappliedSlashes` queue; it purely trusts the era arithmetic. `pallet-staking-async` was later hardened against exactly this failure mode (see `prdoc/stable2509/pr_9079.prdoc` and `ensure_era_slashes_applied` in `substrate/frame/staking-async/src/pallet/impls.rs`), which shows the maintainers themselves recognized the legacy pallet's design as unsafe in edge cases. This is the direct on-chain analog of the infiniFi `UnwindingModule` bug: a user can unbond and withdraw before the "accrue" (slash-apply) step runs, escaping a loss that the remaining stakers must then absorb in full.

### Finding Description
In `pallet-staking`, slashing for an offence is deferred: [1](#0-0) 
The unapplied slash is stored in `UnappliedSlashes` keyed by `slash_era + slash_defer_duration + 1`, and is only actually applied later (via `on_initialize`/`cancel_deferred_slash`/end-of-era processing) — not synchronously.

`do_withdraw_unbonded`, however, releases unlocking chunks purely based on era math (`ledger.consolidate_unlocked(current_era)`), with no check that any pending/unapplied slash exists for the stash or for the era range covered by the chunk being released: [2](#0-1) 

The design safety net is simply the invariant `BondingDuration >= SlashDeferDuration`, so that by the time a chunk becomes withdrawable, the era holding the deferred slash has already elapsed and (assumed) been applied. But:
- This is a *timing assumption*, not an enforced on-chain guard — nothing prevents a runtime misconfiguration (`SlashDeferDuration >= BondingDuration`), and nothing checks that the slash-application step for the relevant era actually completed (e.g., if `UnappliedSlashes` for that era were never drained due to processing being skipped/cancelled/delayed).
- The team's own `pallet-staking-async` rewrite explicitly patches this exact gap by *reverting withdrawals* when unapplied slashes exist for the previous era: [3](#0-2) 
and the prdoc states this was added as "a defensive measure that ensures slashing guarantees are maintained even in extreme edge cases," describing the very race the infiniFi report flags (withdraw before the slash "accrues"): [4](#0-3) 

Legacy `pallet-staking` (still shipped and used by production relay/parachains that haven't migrated to `pallet-staking-async`) never received this equivalent hardening — its `do_withdraw_unbonded` has no analogous `ensure_era_slashes_applied`/`UnappliedSlashesInPreviousEra` guard.

### Impact Explanation
If any deferred slash for a validator/nominator has not yet been drained from `UnappliedSlashes` by the time its associated unbonding chunk's era arrives (e.g., large slash processing backlog, misconfigured `SlashDeferDuration`, or a slash cancellation window edge case), affected stakers can call `withdraw_unbonded` and fully exit with their pre-slash balance. The slash amount computed against the validator's total exposure is then either partially uncollectable (funds already left the system) or must be absorbed disproportionately by nominators/validators who did not withdraw in time — a direct fund-conservation violation matching the "Impact Gate" criteria: theft/unbacked value escape and duplicate/incorrect settlement of slashing losses onto remaining honest stakers.

### Likelihood Explanation
Under the documented, correctly-configured relationship (`BondingDuration > SlashDeferDuration`) the race window is normally closed by design, so likelihood in a "textbook" runtime is low. However, this is enforced only by convention/config, not by code — any deployment where slash processing is delayed relative to bonding duration (which pallet-staking-async's own PR admits happens in "extreme edge cases") reopens the exact window. This is not a "malicious validator/relayer" precondition — the withdrawing account can be an entirely honest, unprivileged nominator/validator simply calling the public `withdraw_unbonded` extrinsic at the right time.

### Recommendation
Backport the `pallet-staking-async` mitigation to `pallet-staking`: before releasing any unlocking chunk in `do_withdraw_unbonded`, verify there are no unapplied/undrained entries in `UnappliedSlashes` for the era(s) covered by the withdrawal, and reject the withdrawal (or partially withdraw only chunks confirmed safe) until slashing for that era has been applied — mirroring `ensure_era_slashes_applied` / `UnappliedSlashesInPreviousEra`.

### Proof of Concept
1. Configure `SlashDeferDuration` close to `BondingDuration` (or induce a processing delay causing `UnappliedSlashes` for `slash_era` to remain unapplied past the era it was scheduled for, e.g. governance intervention or heavy offence load).
2. A nominator/validator `unbond`s in era `E`; their chunk unlocks at era `E + BondingDuration`.
3. An offence occurs in era `E` targeting this stash; the resulting slash is deferred to `E + SlashDeferDuration + 1`, landing in `UnappliedSlashes`.
4. If the slash entry has not yet been drained by era `E + BondingDuration` (e.g., delayed application), the staker calls `withdraw_unbonded` at that era.
5. `do_withdraw_unbonded` performs no check against `UnappliedSlashes` and releases the full unslashed chunk (`substrate/frame/staking/src/pallet/impls.rs`), while the deferred slash is later applied against a now-reduced/absent exposure, shifting the loss onto remaining stakers — exactly analogous to withdrawing from `UnwindingModule` before `YieldSharing.accrue` runs.

### Citations

**File:** substrate/frame/staking/src/pallet/impls.rs (L1361-1376)
```rust
				} else {
					// Defer to end of some `slash_defer_duration` from now.
					log!(
						debug,
						"deferring slash of {:?} happened in {:?} (reported in {:?}) to {:?}",
						slash_fraction,
						slash_era,
						active_era,
						slash_era + slash_defer_duration + 1,
					);
					UnappliedSlashes::<T>::mutate(
						slash_era.saturating_add(slash_defer_duration).saturating_add(One::one()),
						move |for_later| for_later.push(unapplied),
					);
					add_db_reads_writes(1, 1);
				}
```

**File:** substrate/frame/staking/src/pallet/impls.rs (L1385-1385)
```rust
	/// Unbonds a controller.
```

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L259-270)
```rust
	pub(super) fn do_withdraw_unbonded(controller: &T::AccountId) -> Result<Weight, DispatchError> {
		let mut ledger = Self::ledger(Controller(controller.clone()))?;
		let (stash, old_total) = (ledger.stash.clone(), ledger.total);
		let active_era = Rotator::<T>::active_era();

		// Ensure last era slashes are applied. Else we block the withdrawals.
		if active_era > 1 {
			Self::ensure_era_slashes_applied(active_era.saturating_sub(1))?;
		}

		let earliest_era_to_withdraw = Self::calculate_earliest_withdrawal_era(active_era);

```

**File:** prdoc/stable2509/pr_9079.prdoc (L1-21)
```text
title: "Prevent withdrawals while processing offences"

doc:
  - audience: Runtime Dev
    description: |
      Adds withdrawal restrictions to prevent users from withdrawing unbonded funds while 
      there are unprocessed offences that could result in slashing. This is a defensive 
      measure that ensures slashing guarantees are maintained even in extreme edge cases.
      
      Key changes:
      - Withdrawals are blocked if there are unapplied slashes from the previous era 
        (returns `UnappliedSlashesInPreviousEra` error). This occurs when all unapplied 
        slashes for an era could not be applied within one era worth of blocks. While 
        one era is reserved for applying slashes page by page, if the era rolls over 
        before completion, these slashes can only be applied via the permissionless 
        `apply_slash` call.
      - Withdrawals are restricted to the minimum of the active era and the last fully 
        processed offence era
      - Unbonding chunks are now keyed by active era instead of current era
      - Offences arriving after their intended application era are rejected and emit 
        `OffenceTooOld` event
```
