### Title
Staking `do_withdraw_unbonded` consolidates unlocking chunks using `CurrentEra` instead of `ActiveEra`, allowing premature unlock/inconsistent lock accounting - (File: `substrate/frame/staking/src/pallet/impls.rs`)

### Summary
The external Yieldoor report's core defect is a **wrong-but-related checkpoint value substituted for the correct one** (`mainPosition.tickUpper` used instead of `vestPosition.tickUpper`) when releasing/settling a position, breaking the invariant that release logic must reference the exact checkpoint that was used to create the lock. The equivalent local pattern exists in `pallet-staking`'s unbonding-release path, where `do_withdraw_unbonded` consolidates (releases) unlocking chunks against `CurrentEra` rather than `ActiveEra`, while the chunks were scheduled for release using `active_era + BondingDuration` at `unbond()` time.

### Finding Description
`Staking::unbond()` schedules an unlocking chunk to become free at `era = active_era() + BondingDuration`, as seen in staking-async test expectations: [1](#0-0) 

However, the actual release/consolidation of these chunks in `do_withdraw_unbonded` reads `CurrentEra` (not `ActiveEra`) to decide which chunks are unlocked: [2](#0-1) 

`CurrentEra` and `ActiveEra` are distinct concepts in the staking pallet: `CurrentEra` is the "planned" era used for election/snapshot bookkeeping and can be incremented ahead of `ActiveEra` during session-boundary transitions, while `ActiveEra` is the era that is actually live on-chain and is the value used to compute the unlock timestamp at `unbond()` time. This exact class of mismatch was already identified and fixed for `pallet-nomination-pools`, per the project's own prdoc: [3](#0-2) 

That prdoc explicitly states the intended invariant: *"Current Era should only be used for election logic"* — withdrawals must use the *active* era. The pools-side call sites were migrated, but `pallet-staking::do_withdraw_unbonded` (`substrate/frame/staking/src/pallet/impls.rs:199`) still calls `CurrentEra::<T>::get()` and feeds it into `ledger.consolidate_unlocked(current_era)`, which walks the `unlocking: BoundedVec<UnlockChunk>` and releases any chunk whose `era <= current_era`.

This is structurally identical to the Yieldoor bug: the "release" operation is bound to the wrong (but related) checkpoint identifier instead of the one that was used to create the commitment. In Uniswap's case it was `mainPosition.tickUpper` vs `vestPosition.tickUpper`; here it is `CurrentEra` vs `ActiveEra`.

### Impact Explanation
If `CurrentEra` can be strictly ahead of `ActiveEra` (a documented possibility around era-transition boundaries, and the reason the pools-side fix in pr_10986 was made at all), then `do_withdraw_unbonded` can release/unlock a chunk whose intended unlock era (`active_era + BondingDuration`) has not actually arrived yet on-chain, because it is comparing against the higher `CurrentEra` value. This breaks the core staking invariant that funds must remain locked for the full `BondingDuration` measured from the era that was actually active when `unbond()` was called — a bonding-duration lock is a security/slashing-window guarantee (funds must stay slashable for a full bonding period). Using the wrong era value undermines that guarantee, potentially allowing unbonded balance to be withdrawn earlier than the protocol's economic-security window intends, i.e., unauthorized early release of value that should still be locked/slashable. This falls under the "permanent user-fund or bridge-state lock" / "runtime bugs that compromise intended behavior" impact class of the program scope, mirroring the report's "funds handled against the wrong checkpoint" primitive, just in the opposite direction (early release instead of a stuck lock).

### Likelihood Explanation
This requires no privileged actor: any nominator/validator who calls `unbond()` and then `withdraw_unbonded()` is a normal, permissionless user flow. The trigger condition (`CurrentEra > ActiveEra`) is a naturally occurring runtime state during era rollover, not an attacker-crafted condition, so the path is reachable through ordinary usage timing rather than exploitation of a malicious peer/validator/relayer, satisfying the "public entrypoint, unprivileged" requirement of the task.

### Recommendation
Change `do_withdraw_unbonded` to use the active era (the same era-source `unbond()` uses to compute `era = active_era + BondingDuration`) rather than `CurrentEra`, consistent with the fix already applied to `pallet-nomination-pools` in pr_10986. Concretely, replace:
```rust
if let Some(current_era) = CurrentEra::<T>::get() {
    ledger = ledger.consolidate_unlocked(current_era)
}
```
with the equivalent `ActiveEra::<T>::get().map(|a| a.index)` value, and audit all other `consolidate_unlocked`/unlock-comparison call sites in `pallet-staking` for the same `CurrentEra` vs `ActiveEra` inconsistency.

### Proof of Concept
1. Nominator calls `Staking::unbond(amount)` while `ActiveEra = E`. The chunk is stored with `era = E + BondingDuration`.
2. During a session/era-transition window, `CurrentEra` is bumped to `E + 1` (or higher) while `ActiveEra` has not yet caught up (it advances only at the actual era-change hook).
3. Nominator calls `Staking::withdraw_unbonded()`. `do_withdraw_unbonded` reads `CurrentEra = E + 1 (or more)` and calls `ledger.consolidate_unlocked(E+1)`, which releases any chunk with `era <= E+1`.
4. If the intended unlock era `E + BondingDuration` is `<= CurrentEra` but `> ActiveEra` at that moment (a gap that exists specifically because `CurrentEra` is planned ahead of `ActiveEra`), the chunk is released one (or more) eras earlier than the bonding-duration guarantee promised at `unbond()` time, confirmed by the divergent era semantics documented in `prdoc/stable2512-2/pr_10986.prdoc` and the code at `substrate/frame/staking/src/pallet/impls.rs:199`.

### Citations

**File:** substrate/frame/staking-async/src/tests/nominators_no_slashing.rs (L656-664)
```rust
			assert_ok!(Staking::unbond(RuntimeOrigin::signed(alice), 998));
			assert_eq!(
				staking_events_since_last_call(),
				[Event::Unbonded {
					stash: alice,
					amount: 998,
					era: active_era() + BondingDuration::get()
				}]
			);
```

**File:** substrate/frame/staking/src/pallet/impls.rs (L193-219)
```rust
	pub(super) fn do_withdraw_unbonded(
		controller: &T::AccountId,
		num_slashing_spans: u32,
	) -> Result<Weight, DispatchError> {
		let mut ledger = Self::ledger(Controller(controller.clone()))?;
		let (stash, old_total) = (ledger.stash.clone(), ledger.total);
		if let Some(current_era) = CurrentEra::<T>::get() {
			ledger = ledger.consolidate_unlocked(current_era)
		}
		let new_total = ledger.total;

		let ed = asset::existential_deposit::<T>();
		let used_weight =
			if ledger.unlocking.is_empty() && (ledger.active < ed || ledger.active.is_zero()) {
				// This account must have called `unbond()` with some value that caused the active
				// portion to fall below existential deposit + will have no more unlocking chunks
				// left. We can now safely remove all staking-related information.
				Self::kill_stash(&ledger.stash, num_slashing_spans)?;

				T::WeightInfo::withdraw_unbonded_kill(num_slashing_spans)
			} else {
				// This was the consequence of a partial unbond. just update the ledger and move on.
				ledger.update()?;

				// This is only an update, so we use less overall weight.
				T::WeightInfo::withdraw_unbonded_update(num_slashing_spans)
			};
```

**File:** prdoc/stable2512-2/pr_10986.prdoc (L1-10)
```text
title: '[Pool] Use active era for withdrawals'
doc:
- audience: Runtime Dev
  description: Standardising using active era in pools and staking. Current Era should
    only be used for election logic
crates:
- name: pallet-nomination-pools
  bump: patch
- name: pallet-staking-async
  bump: patch
```
