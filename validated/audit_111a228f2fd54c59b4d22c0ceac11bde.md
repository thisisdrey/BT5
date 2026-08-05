Audit Report

## Title
Off-by-one in recent-validator lookback lets a slashed/slashable validator downgrade to fast nominator unbonding one era early - ([File: substrate/frame/staking-async/src/pallet/mod.rs])

## Summary
In `Pallet::unbond`, the recency check for whether a stash was recently a validator uses `active_era.saturating_sub(last_era) < T::BondingDuration::get()`, a strict inequality that excludes the exact boundary case where `active_era - last_era == BondingDuration`. [1](#0-0)  This contradicts the pallet's own documented invariant that nominators who were validators "within `Config::BondingDuration`" of the active era must use the full `BondingDuration`, causing a one-era-early downgrade to `NominatorFastUnbondDuration` at exactly that boundary. [2](#0-1) 

## Finding Description
`unbond()` computes `was_recent_validator` from `LastValidatorEra` using strict `<` rather than `<=`: [3](#0-2)  When `active_era.saturating_sub(last_era) == T::BondingDuration::get()` exactly, this evaluates `false`, so the stash is misclassified as a "pure nominator" and receives `unbond_duration = NominatorFastUnbondDuration` instead of the full `BondingDuration`, which is then stamped into the new `UnlockChunk.era` at line 2011-2012. [4](#0-3)  The comment directly above the check states the intended rule as "within BondingDuration," which naturally includes the boundary of exactly `BondingDuration` eras elapsed, so the implementation deviates from its own documented invariant. The existing regression test suite (`validator_cannot_switch_to_nominator_to_avoid_slashing`) only exercises the `== 0` case and never the `== BondingDuration` boundary, so this misclassification is unguarded by tests.

Because `SlashDeferDuration` is only required to be strictly less than `BondingDuration` (not `<=`), and offence reporting/processing near the bonding-duration deadline is an explicitly documented scenario in this crate, an account switching from validator to nominator and waiting exactly `BondingDuration` eras can, via a single call to the public, unprivileged `unbond` extrinsic, obtain an `UnlockChunk` unlocking one era earlier than the security invariant promises.

## Impact Explanation
This is a runtime bug that compromises intended behavior of a security control specifically designed to prevent validators from evading slashing by switching roles (anti-slash-evasion mechanism in `unbond`). The exact wrong value produced is the `unbond_duration` (and consequently the `UnlockChunk.era` field) computed in `unbond()`, which is under-computed by exactly one era at the documented boundary condition, contradicting the pallet's own invariant. Whether this materializes into actual fund theft depends on separate withdrawal-time guards in `do_withdraw_unbonded` (era-slashes-applied checks), which were not fully traced in this investigation, but the core defect — a provable violation of the documented "within BondingDuration" invariant via an off-by-one comparison — is confirmed directly in the code.

## Likelihood Explanation
No privileged actor is required; any unprivileged stash can trigger this via the public `unbond` extrinsic combined with `nominate()`, waiting a deterministic and publicly observable number of eras to hit the exact boundary.

## Recommendation
Change the comparison from strict `<` to `<=`:
```rust
.map(|last_era| active_era.saturating_sub(last_era) <= T::BondingDuration::get())
```
Add a unit test asserting behavior precisely at `active_era - last_era == BondingDuration`, in addition to the existing `== 0` case.

## Proof of Concept
1. Configure `AreNominatorsSlashable = false`, `BondingDuration = 3`, `NominatorFastUnbondDuration = 2`.
2. Account `A` is a validator in era `E` (`LastValidatorEra::<T>::get(A) == Some(E)`).
3. `A` calls `nominate()`.
4. Advance active era to exactly `E + BondingDuration`.
5. `A` calls `unbond(value)`: `active_era.saturating_sub(E) == BondingDuration`, so `was_recent_validator` is `false`, and the `UnlockChunk` is stamped with `era = active_era + NominatorFastUnbondDuration` instead of `active_era + BondingDuration`, reproducible by extending `validator_cannot_switch_to_nominator_to_avoid_slashing` to the `== BondingDuration` boundary. [5](#0-4)

### Citations

**File:** substrate/frame/staking-async/src/pallet/mod.rs (L1993-2009)
```rust
				// Determine unbonding duration based on validator history.
				// If the account was a validator in recent eras (within BondingDuration), they must
				// wait the full BondingDuration even if they've switched to nominator role.
				// This prevents validators from avoiding slashing by switching roles and using the
				// shorter nominator unbonding period.
				let active_era = session_rotation::Rotator::<T>::active_era();
				let was_recent_validator = LastValidatorEra::<T>::get(&stash)
					.map(|last_era| active_era.saturating_sub(last_era) < T::BondingDuration::get())
					.unwrap_or(false);

				let unbond_duration = if was_recent_validator {
					// Use full bonding duration for recent validators
					T::BondingDuration::get()
				} else {
					// Use nominator bonding duration for pure nominators
					<Self as sp_staking::StakingInterface>::nominator_bonding_duration()
				};
```

**File:** substrate/frame/staking-async/src/pallet/mod.rs (L2011-2023)
```rust
				let era =
					session_rotation::Rotator::<T>::active_era().saturating_add(unbond_duration);
				if let Some(chunk) = ledger.unlocking.last_mut().filter(|chunk| chunk.era == era) {
					// To keep the chunk count down, we only keep one chunk per era. Since
					// `unlocking` is a FiFo queue, if a chunk exists for `era` we know that it will
					// be the last one.
					chunk.value = chunk.value.defensive_saturating_add(value)
				} else {
					ledger
						.unlocking
						.try_push(UnlockChunk { value, era })
						.map_err(|_| Error::<T>::NoMoreChunks)?;
				};
```

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L1869-1884)
```rust
	/// Returns the bonding duration for pure nominators.
	///
	/// This returns the *potential* fast unbonding duration that pure nominators can use:
	/// - When [`AreNominatorsSlashable`] is `true`, returns full [`Config::BondingDuration`]
	/// - When [`AreNominatorsSlashable`] is `false`, returns
	///   [`Config::NominatorFastUnbondDuration`]
	///
	/// **Important**: The actual unbonding duration for a specific account is determined in
	/// `unbond()` based on validator history (see [`LastValidatorEra`]):
	/// - Validators always use full [`Config::BondingDuration`]
	/// - Nominators who were validators in recent eras (within [`Config::BondingDuration`]) use
	///   full [`Config::BondingDuration`] to ensure they can be slashed for past offences
	/// - Pure nominators use the value returned by this function
	fn nominator_bonding_duration() -> EraIndex {
		if AreNominatorsSlashable::<T>::get() {
			T::BondingDuration::get()
```

**File:** substrate/frame/staking-async/src/tests/nominators_no_slashing.rs (L653-691)
```rust
			// Step 3: Alice (now a nominator) unbonds partially. We leave `active` exactly at
			// `MinNominatorBond` (= ED + 1 under the default builder) so the unbond passes the
			// per-role guard.
			assert_ok!(Staking::unbond(RuntimeOrigin::signed(alice), 998));
			assert_eq!(
				staking_events_since_last_call(),
				[Event::Unbonded {
					stash: alice,
					amount: 998,
					era: active_era() + BondingDuration::get()
				}]
			);

			// Alice should still be a nominator
			assert!(Nominators::<Test>::contains_key(&alice));

			// Calculate expected unlock eras:
			// - Fast unbond: current_era (1) + NominatorFastUnbondDuration (2) = 3
			// - Full unbond: current_era (1) + BondingDuration (3) = 4
			let fast_unbond_era = 1 + NominatorFastUnbondDuration::get();
			let validator_unbond_era = 1 + BondingDuration::get();

			assert_eq!(NominatorFastUnbondDuration::get(), 2);
			assert_eq!(BondingDuration::get(), 3);
			assert_eq!(fast_unbond_era, 3);
			assert_eq!(validator_unbond_era, 4);

			// Alice must use full bonding duration despite being a nominator now,
			// because she was a validator in era 1 (within BondingDuration of active era).
			// This prevents her from withdrawing before the slash is applied.
			assert_eq!(
				Staking::ledger(alice.into()).unwrap(),
				StakingLedgerInspect {
					stash: alice,
					total: 1000,
					active: 2,
					unlocking: bounded_vec![UnlockChunk { value: 998, era: validator_unbond_era }],
				}
			);
```
