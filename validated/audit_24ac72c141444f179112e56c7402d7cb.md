## Finding

### Title
`pallet-staking::withdraw_unbonded` lacks the unapplied/unprocessed-slash guard added to `pallet-staking-async`, letting a staker withdraw before a deferred slash for their era is applied - (File: `substrate/frame/staking/src/pallet/impls.rs`)

### Summary
The Morph report's core broken invariant is: a staker can complete `withdraw` before a slash that should apply to their stake is settled, because the withdrawal timer and the slash-settlement timer are decoupled and no cross-check ties them together. The same broken invariant exists in `pallet-staking`'s classic `do_withdraw_unbonded`, which the Polkadot SDK team itself identified and fixed — but only in the newer `pallet-staking-async` crate.

### Finding Description
In `pallet-staking-async`, `do_withdraw_unbonded` explicitly blocks withdrawal until all slashes that could apply to the withdrawing era are settled: [1](#0-0) 

This relies on `ensure_era_slashes_applied` (checks `UnappliedSlashes` for the previous era) and `calculate_earliest_withdrawal_era` (checks `OffenceQueueEras` for offences not yet processed), documented as a defensive fix for the exact class of bug described in the external report: [2](#0-1) 

By contrast, the classic `pallet-staking::do_withdraw_unbonded` (still the production staking pallet for the relay chain) performs no such check — it only filters unlocking chunks by `chunk.era <= current_era` via `consolidate_unlocked`, with zero reference to `UnappliedSlashes`, `SlashingSpans` deferral state, or offence-processing backlog: [3](#0-2) 

Correctness in the classic pallet depends entirely on the *unenforced* configuration convention that `SlashDeferDuration < BondingDuration`, and on offences always being reported/processed well inside that window. Nothing in `do_withdraw_unbonded` or `withdraw_unbonded` re-validates this relationship at call time: [4](#0-3) 

The existing regression test `staker_cannot_bail_deferred_slash` only demonstrates the happy path where `SlashDeferDuration=2` is comfortably less than `BondingDuration=3` and offence processing is instantaneous. It does not cover the backlog/delayed-reporting scenario that `pallet-staking-async`'s PR 9079 was written specifically to defend against ("if all unapplied slashes for an era could not be applied within one era worth of blocks... these slashes can only be applied via the permissionless `apply_slash` call"). The classic pallet has no `apply_slash`-style fallback or withdrawal gate at all for this backlog case: [5](#0-4) 

### Impact Explanation
If offence reporting/processing for an era is delayed (e.g., heavy offence volume, chain congestion, or a validator/nominator timing their unbond right before committing an equivocation near the edge of the bonding window), a staker can call `withdraw_unbonded` and have their unbonded stake ledger consolidated and unlocked to free balance before the corresponding deferred slash is ever applied to that ledger. Because slashing in `pallet-staking` reduces a live `StakingLedger`'s `active`/`unlocking` balance, once the ledger is emptied and the stash reaped (`kill_stash`), the slash has nothing left to act on — the offender effectively escapes the intended punishment, breaking the "runtime bug compromising intended behavior" invariant (funds that should have been slashed are instead fully returned to the attacker). This directly parallels the Morph `L1Staking.sol` accounting break where a staker withdraws before their challenged/slashable state resolves.

### Likelihood Explanation
This requires no privileged actor, relayer, governance, or malicious peer — it is exploitable by any unprivileged staker who: (1) commits or is exposed to a slashable offence, (2) unbonds immediately, and (3) calls `withdraw_unbonded` once `BondingDuration` eras have elapsed, betting that offence reporting/processing/deferred-slash-application for their era lags behind. The classic pallet's mitigation is purely a runtime-configuration convention (`SlashDeferDuration < BondingDuration`) with no on-chain enforcement or fallback for backlog conditions, and Parity's own fix commentary in PR 9079 explicitly calls this "extreme edge cases" that "should not occur under normal operation" but nonetheless required a dedicated fix in the successor pallet — confirming the underlying scenario is real and previously unguarded.

### Recommendation
Port the `pallet-staking-async` withdrawal guard back into `pallet-staking::do_withdraw_unbonded`: before consolidating unlocking chunks, verify there are no `UnappliedSlashes` for the previous era and cap `earliest_era_to_withdraw` by the oldest era with unprocessed/undeferred offences, mirroring `ensure_era_slashes_applied` and `calculate_earliest_withdrawal_era` in `substrate/frame/staking-async/src/pallet/impls.rs`.

### Proof of Concept
Conceptual reproduction against `pallet-staking` (classic):
1. Staker `S` is a validator/nominator with `BondingDuration = B` and `SlashDeferDuration = D` (`D < B` by convention only).
2. `S` commits (or is otherwise exposed to) an offence in era `E`, but reporting/processing of that offence's `on_offence` call is delayed until near the edge of the deferral window (era `E + D - ε`), e.g. due to backlog of many simultaneous offences (same technique used in `withdrawals_are_blocked_for_unprocessed_and_unapplied_slashes` to flood the pipeline for `pallet-staking-async`).
3. `S` calls `unbond` in era `E`, scheduling `withdraw_unbonded` eligibility at era `E + B`.
4. If `E + B <= E + D` is violated in practice (backlog pushes actual slash application past `E + D`, or `D` is misconfigured close to `B`), `S` calls `withdraw_unbonded` at era `E + B`; `do_withdraw_unbonded` only checks `chunk.era <= current_era` via `consolidate_unlocked` — with no check against `UnappliedSlashes` — so the withdrawal succeeds and the stash may be reaped via `kill_stash` before the deferred slash is applied, resulting in the slash being silently lost.

### Citations

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L259-280)
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

		log!(
			debug,
			"Withdrawing unbonded stake. Active_era is: {:?} | \
			Earliest era we can allow withdrawing: {:?}",
			active_era,
			earliest_era_to_withdraw
		);

		// withdraw unbonded balance from the ledger until earliest_era_to_withdraw.
		ledger = ledger.consolidate_unlocked(earliest_era_to_withdraw);
```

**File:** prdoc/stable2509/pr_9079.prdoc (L1-26)
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
      
      Both the `UnappliedSlashesInPreviousEra` error and withdrawal restrictions due to 
      delayed offence processing are extremely rare scenarios that should not occur under 
      normal operation. These are defensive measures to handle edge cases where slash 
      processing is delayed beyond expected timelines.
```

**File:** substrate/frame/staking/src/pallet/impls.rs (L193-233)
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

		// `old_total` should never be less than the new total because
		// `consolidate_unlocked` strictly subtracts balance.
		if new_total < old_total {
			// Already checked that this won't overflow by entry condition.
			let value = old_total.defensive_saturating_sub(new_total);
			Self::deposit_event(Event::<T>::Withdrawn { stash, amount: value });

			// notify listeners.
			T::EventListeners::on_withdraw(controller, value);
		}

		Ok(used_weight)
	}
```

**File:** substrate/frame/staking/src/pallet/mod.rs (L1312-1322)
```rust
		#[pallet::call_index(3)]
		#[pallet::weight(T::WeightInfo::withdraw_unbonded_kill(*num_slashing_spans))]
		pub fn withdraw_unbonded(
			origin: OriginFor<T>,
			num_slashing_spans: u32,
		) -> DispatchResultWithPostInfo {
			let controller = ensure_signed(origin)?;

			let actual_weight = Self::do_withdraw_unbonded(&controller, num_slashing_spans)?;
			Ok(Some(actual_weight).into())
		}
```

**File:** substrate/frame/staking/src/tests.rs (L2984-3051)
```rust
#[test]
fn staker_cannot_bail_deferred_slash() {
	// as long as SlashDeferDuration is less than BondingDuration, this should not be possible.
	ExtBuilder::default().slash_defer_duration(2).build_and_execute(|| {
		mock::start_active_era(1);

		assert_eq!(asset::stakeable_balance::<Test>(&11), 1000);
		assert_eq!(asset::stakeable_balance::<Test>(&101), 2000);

		let exposure = Staking::eras_stakers(active_era(), &11);
		let nominated_value = exposure.others.iter().find(|o| o.who == 101).unwrap().value;

		on_offence_now(&[offence_from(11, None)], &[Perbill::from_percent(10)]);

		// now we chill
		assert_ok!(Staking::chill(RuntimeOrigin::signed(101)));
		assert_ok!(Staking::unbond(RuntimeOrigin::signed(101), 500));

		assert_eq!(CurrentEra::<Test>::get().unwrap(), 1);
		assert_eq!(active_era(), 1);

		assert_eq!(
			Ledger::<Test>::get(101).unwrap(),
			StakingLedgerInspect {
				active: 0,
				total: 500,
				stash: 101,
				legacy_claimed_rewards: bounded_vec![],
				unlocking: bounded_vec![UnlockChunk { era: 4u32, value: 500 }],
			}
		);

		// no slash yet.
		assert_eq!(asset::stakeable_balance::<Test>(&11), 1000);
		assert_eq!(asset::stakeable_balance::<Test>(&101), 2000);

		// no slash yet.
		mock::start_active_era(2);
		assert_eq!(asset::stakeable_balance::<Test>(&11), 1000);
		assert_eq!(asset::stakeable_balance::<Test>(&101), 2000);
		assert_eq!(CurrentEra::<Test>::get().unwrap(), 2);
		assert_eq!(active_era(), 2);

		// no slash yet.
		mock::start_active_era(3);
		assert_eq!(asset::stakeable_balance::<Test>(&11), 1000);
		assert_eq!(asset::stakeable_balance::<Test>(&101), 2000);
		assert_eq!(CurrentEra::<Test>::get().unwrap(), 3);
		assert_eq!(active_era(), 3);

		// and cannot yet unbond:
		assert_storage_noop!(assert!(
			Staking::withdraw_unbonded(RuntimeOrigin::signed(101), 0).is_ok()
		));
		assert_eq!(
			Ledger::<Test>::get(101).unwrap().unlocking.into_inner(),
			vec![UnlockChunk { era: 4u32, value: 500 as Balance }],
		);

		// at the start of era 4, slashes from era 1 are processed,
		// after being deferred for at least 2 full eras.
		mock::start_active_era(4);

		assert_eq!(asset::stakeable_balance::<Test>(&11), 900);
		assert_eq!(asset::stakeable_balance::<Test>(&101), 2000 - (nominated_value / 10));

		// and the leftover of the funds can now be unbonded.
	})
```
