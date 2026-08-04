### Title
Off-by-one in recent-validator lookback lets a slashed/slashable validator downgrade to fast nominator unbonding one era early - ([File: substrate/frame/staking-async/src/pallet/mod.rs])

### Summary
`Pallet::unbond` in `pallet-staking-async` decides whether a stash must use the full `BondingDuration` (validator rule, protects against slash evasion) or the shorter `NominatorFastUnbondDuration` (only for "pure" nominators when `AreNominatorsSlashable` is false), based on how recently the stash acted as a validator. The recency check uses a strict `<` instead of `<=`, one era earlier than the documented/intended boundary, mirroring the reported `WithdrawalEscrow` bug where `block.timestamp > exitWindowStart` should have been `>=`. [1](#0-0) 

### Finding Description
In `unbond()`, the pallet computes:

```rust
let active_era = session_rotation::Rotator::<T>::active_era();
let was_recent_validator = LastValidatorEra::<T>::get(&stash)
    .map(|last_era| active_era.saturating_sub(last_era) < T::BondingDuration::get())
    .unwrap_or(false);
``` [2](#0-1) 

The stated invariant in the doc comment is: *"Nominators who were validators in recent eras (within `Config::BondingDuration`) use full `Config::BondingDuration`."* [3](#0-2)  "Within `BondingDuration` eras" naturally includes the boundary case where exactly `BondingDuration` eras have elapsed since the account was last a validator (`active_era - last_era == BondingDuration`). At that exact boundary the strict `<` comparison evaluates to `false`, so the stash is treated as a "pure nominator" and is granted `T::NominatorFastUnbondDuration` (e.g. 2 eras) instead of the full `T::BondingDuration` (e.g. 3 eras) for this specific unbond call, i.e. one era earlier than intended.

This matters because `SlashDeferDuration` can validly be configured up to (but not including) `BondingDuration` (`integrity_test` only asserts `SlashDeferDuration < BondingDuration`) [4](#0-3) , and offence reporting/processing can itself be delayed (the crate's own docs describe reports arriving very close to the bonding-duration deadline) [5](#0-4) . A validator can therefore switch to nominator (`nominate()`), wait until `active_era - last_validator_era == BondingDuration` exactly, and call `unbond()` to get the shorter, fast-unbond era stamped on the new `UnlockChunk`, one era before the protection the comment promises. The existing regression test `validator_cannot_switch_to_nominator_to_avoid_slashing` only exercises the strictly-less-than case (`active_era.saturating_sub(last_era) == 0`), never the exact-equality boundary, so the off-by-one is unguarded by tests. [6](#0-5) 

### Impact Explanation
This directly targets the "staking or asset accounting" and "runtime bugs that compromise intended behavior" impact categories: the unbonding-duration classification is a security control explicitly designed to prevent validators from evading slashing by switching roles, and the boundary error weakens that control by one era for every stash whose validator history sits exactly at the `BondingDuration` threshold. Whether this is independently exploitable to actually escape a real slash also depends on `do_withdraw_unbonded`'s separate `calculate_earliest_withdrawal_era`/`ensure_era_slashes_applied` guards, which act as a second line of defense at withdrawal time using global offence-queue state rather than the (possibly mis-classified) per-chunk unlock era. Because I could not fully trace every interaction between a shortened `UnlockChunk.era` and those withdrawal-time guards within the remaining investigation budget, I cannot confirm end-to-end fund theft; the concrete, provable defect is that the "recent validator" boundary check silently misclassifies the exact-equality case, contradicting the pallet's own documented invariant and its own comment ("within BondingDuration").

### Likelihood Explanation
No privileged actor is required — this is triggerable by any unprivileged nominator/validator stash via the public, non-privileged `unbond` extrinsic and requires only waiting the correct number of eras (era boundaries are deterministic and publicly observable), analogous to how the Sherlock report notes the timestamp boundary in PoS Ethereum is predictable due to fixed slot times. No malicious peer, relayer, validator, or governance action is needed.

### Recommendation
Change the comparison from strict `<` to `<=` (or equivalently compare against `BondingDuration.saturating_sub(1)`), so that exact equality (`active_era - last_era == BondingDuration`) is still treated as "recent validator" and forced to use the full `BondingDuration`:
```rust
.map(|last_era| active_era.saturating_sub(last_era) <= T::BondingDuration::get())
```
Add a dedicated unit test asserting behavior precisely at `active_era - last_era == BondingDuration` (in addition to the existing `== 0` case) to lock in the corrected boundary.

### Proof of Concept
1. Set `AreNominatorsSlashable = false`, `BondingDuration = 3`, `NominatorFastUnbondDuration = 2`.
2. Account `A` is a validator in era `E` (`LastValidatorEra::<T>::get(A) == Some(E)`).
3. `A` calls `nominate()` to switch role to nominator.
4. Advance active era until `active_era == E + BondingDuration` (exactly the boundary).
5. `A` calls `unbond(value)`. `active_era.saturating_sub(E) == BondingDuration`, so `was_recent_validator` evaluates `false` due to strict `<`, and `unbond_duration = NominatorFastUnbondDuration` (2) instead of `BondingDuration` (3); the new `UnlockChunk` is stamped with `era = active_era + 2` instead of `active_era + 3`.
6. Compare against `validator_cannot_switch_to_nominator_to_avoid_slashing`, which only tests `active_era.saturating_sub(last_era) == 0` [7](#0-6)  — extending that test to the `== BondingDuration` boundary reproduces the misclassification and demonstrates the chunk unlocks one era earlier than the pallet's documented guarantee.

### Citations

**File:** substrate/frame/staking-async/src/pallet/mod.rs (L1786-1791)
```rust
			assert!(
				T::SlashDeferDuration::get() < T::BondingDuration::get() || T::BondingDuration::get() == 0,
				"As per documentation, slash defer duration ({}) should be less than bonding duration ({}).",
				T::SlashDeferDuration::get(),
				T::BondingDuration::get(),
			);
```

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

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L1869-1888)
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
		} else {
			T::NominatorFastUnbondDuration::get()
		}
	}
```

**File:** substrate/frame/staking-async/src/lib.rs (L169-196)
```rust
//! **Withdrawal Timeline Example with an Offence**:
//! ```text
//! Era:        90    91    92    93    94    95    96    97    98    99    100   ...  117   118
//!             |     |     |     |     |     |     |     |     |     |     |          |     |
//! Unbond:     U
//! Offence:    X
//! Reported:               R
//! Processed:              P (within next few blocks)
//! Slash Applied:                                                                       S
//! Withdraw:                                                                            ❌    ✓
//!
//! With BondingDuration = 28 and SlashDeferDuration = 27:
//! - User unbonds in era 90
//! - Offence occurs in era 90
//! - Reported in era 92 (typically within 2 days, but reportable until Era 116)
//! - Processed in era 92 (within next few blocks after reporting)
//! - Slash deferred for 27 eras, applied at era 117 (90 + 27)
//! - Cannot withdraw unbonded chunks until era 118 (90 + 28)
//!
//! The 28-era bonding duration ensures that any offences committed before or during
//! unbonding have time to be reported, processed, and applied before funds can be
//! withdrawn. This provides a window for governance to cancel slashes that may have
//! resulted from software bugs.
//! ```
//!
//! **Key Restrictions**:
//! 1. Cannot withdraw if previous era has unapplied slashes
//! 2. Cannot withdraw funds from eras with unprocessed offences
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
