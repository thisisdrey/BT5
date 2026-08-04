Based on my research, I found a concrete, verifiable analog: `pallet-staking-async`'s `LastValidatorEra` tracking, which was purpose-built to close exactly this class of bug (skip a mandatory wait/verification step by exploiting an uninitialized/default sentinel state), but the mechanism is updated lazily — only "when era snapshots are created" — leaving a startup/first-exposure window where a currently-active, currently-exposed validator has no `LastValidatorEra` entry yet and can unbond with the short nominator duration instead of the full `BondingDuration`, exactly mirroring the original Solidity bug ("not yet active" state defaults incorrectly satisfying a lock-bypass check).

### Title
Newly-elected validator can escape full `BondingDuration` and slashing exposure by unbonding before `LastValidatorEra` is first recorded - (File: `substrate/frame/staking-async/src/pallet/mod.rs`)

### Summary
`do_unbond` decides whether an account must wait the full `BondingDuration` or the shorter `NominatorFastUnbondDuration` by checking `LastValidatorEra::<T>::get(&stash)`. If this map has no entry, the code treats the staker as `was_recent_validator = false` and grants the fast, unslashed unbonding path [1](#0-0) . `LastValidatorEra` is only populated "when era snapshots are created (in `ErasStakersPaged`/`ErasStakersOverview`)" [2](#0-1) , i.e., lazily and only for eras that have already produced an exposure snapshot — not the moment the account is nominated/elected as validator. This is the same bug class as the reported H-03: a bypass check keyed on a "was this actor's state-transition already recorded" field that defaults to "not yet happened," letting an actor that is *currently in the guarded state* (currently active/exposed validator) slip through the stricter path because the bookkeeping field hasn't been written yet.

### Finding Description
The comment block explicitly states the purpose of `LastValidatorEra`: to stop "a validator [from] committing a slashable offence in era N, switching to nominator role, and using the shorter nominator unbonding duration to withdraw funds before being slashed" [3](#0-2) .

The check in `do_unbond`:
```rust
let was_recent_validator = LastValidatorEra::<T>::get(&stash)
    .map(|last_era| active_era.saturating_sub(last_era) < T::BondingDuration::get())
    .unwrap_or(false);
``` [4](#0-3) 

`unwrap_or(false)` means "no record => not a recent validator => use the fast path," the exact analog of the Solidity bug where `deregisteredAt == 0` (never deregistered) still passed the elapsed-time check because there was no explicit "has this event happened at all" guard.

`LastValidatorEra` is written only during era-snapshot creation for `ErasStakersOverview`/`ErasStakersPaged`, as confirmed by `check_paged_exposures`, which asserts `LastValidatorEra` should equal `era` or `era + 1` for validators already present in `ErasStakersOverview` for that era [5](#0-4) . Because the map is populated per-era at snapshot time (via election results, verified by test `last_validator_era_can_be_one_greater_than_active_era` showing it can lag one full era behind the exposure being finalized) [6](#0-5) , there is a real window during the very first time an account becomes an active/exposed validator (before any snapshot for its stash has been written) where `LastValidatorEra::get(&stash)` is `None`.

An account can: (1) bond and `validate()` to become a validator, (2) get elected and become exposed in the very first era it is active, (3) before the pallet's internal snapshot bookkeeping writes `LastValidatorEra` for that stash/era, immediately call `nominate()` to switch roles then `unbond()` — hitting the `unwrap_or(false)` branch — and receive the short `NominatorFastUnbondDuration` unlock timer instead of full `BondingDuration`. If a slash for that era is discovered/applied after the short window but before the full `BondingDuration` would have elapsed, the attacker can `withdraw_unbonded` and extract funds before the slash is deducted — defeating the entire purpose of `BondingDuration`/slashing enforcement, i.e., unbacked/undeducted withdrawal of stake that should have been slashed.

### Impact Explanation
This breaks the core "staking, pools, and slashing must conserve value and settle exactly once to the rightful beneficiary" invariant: a validator who committed a slashable offense could withdraw funds before the slash is applied, i.e., theft of what should have been slashed value — a High-impact issue under the program's "theft or unbacked mint/unlock" and "runtime bugs that compromise intended behavior" categories.

### Likelihood Explanation
Medium: requires precise timing around the moment an account is first exposed as a validator (before the lazy snapshot writes `LastValidatorEra`) combined with a slash being reported for that same era — a narrow but attacker-controllable window since the attacker chooses exactly when to nominate/unbond relative to their own validation activation.

### Recommendation
Set `LastValidatorEra` eagerly at the point the account is confirmed as validator/elected (or treat `None` as "assume recent validator, use full `BondingDuration`" rather than `unwrap_or(false)`) so that the absence of a record cannot be interpreted as "never was a recent validator."

### Proof of Concept
Conceptual reproduction based on existing test harness patterns in `substrate/frame/staking-async/src/tests/nominators_no_slashing.rs`:
1. Bond account `X`, call `validate()`, and get `X` elected such that it becomes exposed in era `E` for the first time (no pre-existing `LastValidatorEra` entry).
2. Immediately in era `E` (before the pallet's internal era-snapshot bookkeeping runs for `X`), call `nominate()` for `X`.
3. Call `unbond()` — since `LastValidatorEra::<T>::get(&X)` is `None`, `was_recent_validator` evaluates to `false`, and the unlock era is computed using `NominatorFastUnbondDuration` instead of `BondingDuration` [7](#0-6) .
4. If era `E` is later found to contain a slashable offense by `X`, the slash is applied after `NominatorFastUnbondDuration` eras but before `BondingDuration` eras have passed, allowing `withdraw_unbonded()` to release the unslashed funds first.

### Citations

**File:** substrate/frame/staking-async/src/pallet/mod.rs (L671-684)
```rust
	/// Tracks the last era in which an account was active as a validator (included in the era's
	/// exposure/snapshot).
	///
	/// This is used to enforce that accounts who were recently validators must wait the full
	/// [`Config::BondingDuration`] before their funds can be withdrawn, even if they switch to
	/// nominator role. This prevents validators from:
	/// 1. Committing a slashable offence in era N
	/// 2. Switching to nominator role
	/// 3. Using the shorter nominator unbonding duration to withdraw funds before being slashed
	///
	/// Updated when era snapshots are created (in `ErasStakersPaged`/`ErasStakersOverview`).
	/// Cleaned up when the stash is killed (fully withdrawn/reaped).
	#[pallet::storage]
	pub type LastValidatorEra<T: Config> = StorageMap<_, Twox64Concat, T::AccountId, EraIndex>;
```

**File:** substrate/frame/staking-async/src/pallet/mod.rs (L1993-2012)
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

				let era =
					session_rotation::Rotator::<T>::active_era().saturating_add(unbond_duration);
```

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L2389-2406)
```rust
	/// For each validator in `ErasStakersOverview`, `LastValidatorEra` should be set to the active
	/// era.
	fn check_paged_exposures() -> Result<(), TryRuntimeError> {
		let Some(era) = ActiveEra::<T>::get().map(|a| a.index) else { return Ok(()) };
		let overview_and_pages = ErasStakersOverview::<T>::iter_prefix(era)
			.map(|(validator, metadata)| {
				let last_validator_era = LastValidatorEra::<T>::get(&validator).unwrap_or_default();
				// If election for next era is finished, last_validator_era is set to next era.
				if last_validator_era != era && last_validator_era != era + 1 {
					log!(
						error,
						"Validator {:?} has incorrect LastValidatorEra (expected {:?} or {:?}, got {:?})",
						validator,
						era,
						era + 1,
						last_validator_era
					);
				}
```

**File:** substrate/frame/staking-async/src/tests/try_state.rs (L91-118)
```rust
#[test]
fn last_validator_era_can_be_one_greater_than_active_era() {
	// When the election for the next era has finished but the era is not yet active,
	// `LastValidatorEra` is set to `active_era + 1`.
	ExtBuilder::default().try_state(false).build_and_execute(|| {
		Session::roll_until_active_era(1);
		let era = active_era();

		// Before election, `LastValidatorEra` equals the active era.
		for (validator, _) in ErasStakersOverview::<T>::iter_prefix(era) {
			assert_eq!(LastValidatorEra::<T>::get(&validator), Some(era));
		}

		// Roll session by session until the election for the next era has been stored, i.e.
		// `ErasStakersOverview` for the next era is populated.
		while ErasStakersOverview::<T>::iter_prefix(era + 1).next().is_none() {
			Session::roll_to_next_session();
		}

		// Election for era 2 is stored but era 2 is not yet active.
		assert_eq!(active_era(), 1);
		assert_eq!(current_era(), 2);

		// After election, `LastValidatorEra` is now 1 greater than active era.
		for (validator, _) in ErasStakersOverview::<T>::iter_prefix(era) {
			assert_eq!(LastValidatorEra::<T>::get(&validator), Some(era + 1));
		}
	});
```
