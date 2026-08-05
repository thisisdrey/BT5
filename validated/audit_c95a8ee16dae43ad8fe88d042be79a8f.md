Audit Report

## Title
Off-by-one boundary check in `was_recent_validator` lets recently-slashable validators unlock stake one era early via `unbond()` - (File: substrate/frame/staking-async/src/pallet/mod.rs)

## Summary
`Pallet::unbond()` decides whether a stash must wait the full `BondingDuration` (recent-validator protection) or the shorter `nominator_bonding_duration` by comparing `active_era.saturating_sub(last_era) < T::BondingDuration::get()`. This strict `<` comparison causes the protection to lapse exactly one era earlier than the invariant documented for `LastValidatorEra` promises, letting a former validator obtain the shorter nominator unbonding schedule at the precise boundary era.

## Finding Description
`LastValidatorEra` is documented to force former validators to wait the full `T::BondingDuration` before withdrawal, "even if they've switched to nominator role," specifically to prevent evading slashing consequences via the shorter nominator unbonding period. [1](#0-0) 

In `unbond()`, the code computes: [2](#0-1) 

`was_recent_validator` becomes `false` the moment `active_era - last_era == T::BondingDuration::get()`. At that exact era, `unbond_duration` falls back to `nominator_bonding_duration()`, which is a shorter duration, and the resulting unlock era (`active_era + unbond_duration`) is earlier than `active_era + BondingDuration` — undercutting the protection window by exactly one era at that boundary compared to intent implied by the comment ("must wait the full BondingDuration").

The value of `LastValidatorEra` at the boundary is reachable and legitimate, as shown by the dedicated regression test confirming `LastValidatorEra` can be `active_era + 1` right after election but before era activation: [3](#0-2) 

This boundary condition (`active_era - last_era == BondingDuration`) is reachable simply by waiting `BondingDuration` eras after a validator's last exposure and then calling the public, unprivileged `unbond()` extrinsic.

## Impact Explanation
This matches "runtime bug that compromises intended behavior" under the accepted impact classes: the corrupted value is the boolean `was_recent_validator`, which resolves to `false` one era earlier than the comment-documented invariant "must wait the full BondingDuration" implies. The practical impact is that a former validator can select the shorter `nominator_bonding_duration` unlock schedule instead of the full `BondingDuration`, allowing withdrawal of stake earlier than the protection window's documented intent — one era's worth of early unlock at the exact boundary, not an unbounded number of eras. This is a boundary-condition discrepancy between the code and its own doc comment, and represents a genuine deviation from documented intended behavior in scoped pallet code, reachable by any unprivileged staker via the public `unbond()` extrinsic.

## Likelihood Explanation
No privileged actor is required. Any staker who was previously a validator (or nominator with `LastValidatorEra` set) can trigger this by calling the public, unprivileged `unbond()` extrinsic in the exact era where `active_era - LastValidatorEra == BondingDuration`, a deterministic condition based only on public on-chain state (`ActiveEra`, `LastValidatorEra`).

## Recommendation
Change the boundary comparison to be inclusive of the full `BondingDuration` window:

```diff
- .map(|last_era| active_era.saturating_sub(last_era) < T::BondingDuration::get())
+ .map(|last_era| active_era.saturating_sub(last_era) <= T::BondingDuration::get())
```

## Proof of Concept
1. Account `S` validates and is exposed in era `E`, so `LastValidatorEra::<T>::get(S) == Some(E)`.
2. `S` chills / becomes a nominator.
3. Chain progresses until `active_era == E + BondingDuration` exactly.
4. `S` calls `Staking::unbond(origin, value)`. `active_era.saturating_sub(last_era) == BondingDuration`, so the `<` comparison is `false`, `was_recent_validator = false`.
5. `unbond_duration` becomes `nominator_bonding_duration()` (shorter than `BondingDuration`), and the unlock chunk's era is set to `active_era + nominator_bonding_duration` instead of `active_era + BondingDuration`.
6. `S` can call `withdraw_unbonded()` earlier than the full `BondingDuration` protection window intends, at this exact boundary era.

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
