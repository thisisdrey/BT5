Audit Report

## Title
`Staking::nominate` accepts unregistered accounts as nomination targets, violating the "targets must be registered validators" invariant - (File: `substrate/frame/staking/src/pallet/mod.rs`)

## Summary
`Pallet::nominate` validates each nomination target using `!Validators::<T>::get(&n).blocked`, which reads the `Validators` `StorageMap` and returns a zeroed default (`blocked: false`) for any account that never called `validate()`. Because there is no `Validators::<T>::contains_key(&n)` check, an unprivileged, signed, bonded account can nominate arbitrary non-validator accounts, corrupting the `Nominators::<T>::targets` invariant that all listed targets are live, registered validators.

## Finding Description
The registration path `Staking::validate` inserts the caller into `Validators` via `Self::do_add_validator(stash, prefs)`. [1](#0-0) 

`Staking::nominate`'s target guard only checks the `blocked` flag of the `ValidatorPrefs` returned by `Validators::<T>::get(&n)`:
```rust
if old.contains(&n) || !Validators::<T>::get(&n).blocked {
    Ok(n)
} else {
    Err(Error::<T>::BadTarget.into())
}
``` [2](#0-1) 

Since `Validators` is a `StorageMap`, an absent key returns `ValidatorPrefs::default()` (`commission: 0, blocked: false`), so any never-registered account `n` passes the `!blocked` check identically to a real, unblocked validator. No `contains_key` existence check exists anywhere in this function, so `Self::do_add_nominator(stash, nominations)` is called with a target that never went through `validate()`. [3](#0-2) 

This is confirmed by the repository's own fix note for the equivalent path in `pallet-staking-async`, describing exactly this defect ("calling nominate on a validator that doesn't exist silently succeeds") and noting existing tests "were simulating elections with unregistered validators." [4](#0-3)  The corresponding corrected test in `pallet-staking-async` expects `Error::<Test>::BadTarget` when nominating non-validator account `41`. [5](#0-4)  That fix was scoped only to `pallet-staking-async`; the legacy `pallet-staking` crate in this repository (`substrate/frame/staking/src/pallet/mod.rs`) still contains the original faulty check.

## Impact Explanation
This is a runtime logic bug that compromises intended pallet behavior: it breaks the invariant that `Nominators::<T>::targets` only references validators that actually registered via `validate()`. Concretely: nominators can unknowingly commit their bounded `NominationsQuota` slots to phantom targets that can never be elected, since election candidate selection only considers entries present in `Validators`; if all listed targets are non-existent, the nominator's bonded stake earns zero rewards despite having paid for the `nominate` extrinsic and locking funds. This matches the "runtime bugs that compromise intended behavior" category of the impact gate — the corrupted value being the `Nominators::<T>::targets` list, which is allowed to contain account IDs never present in the `Validators` storage map.

## Likelihood Explanation
The bug is trivially and deterministically reachable by any signed, bonded account calling the public `nominate` extrinsic with an arbitrary unregistered `AccountId` as a target — no governance, privileged role, relayer, or malicious-peer assumption is required, and it reproduces on every call.

## Recommendation
Add an explicit existence check before accepting a nomination target:
```rust
if old.contains(&n) || (Validators::<T>::contains_key(&n) && !Validators::<T>::get(&n).blocked) {
    Ok(n)
} else {
    Err(Error::<T>::BadTarget.into())
}
```
This mirrors the fix already shipped for `pallet-staking-async` (`pr_8436`) and should be backported to the legacy `pallet-staking` crate.

## Proof of Concept
1. Bond an account `1` via `Staking::bond(...)`.
2. Call `Staking::nominate(RuntimeOrigin::signed(1), vec![99])` where account `99` never called `Staking::validate(...)`.
3. Observe the call succeeds and `Nominators::<T>::get(1)` now lists target `99`, while `Validators::<T>::contains_key(&99)` is `false` — confirmed by the analogous corrected test `nominating_non_validators_is_not_ok` in `pallet-staking-async`, which asserts `Error::<Test>::BadTarget` for the fixed behavior but which the legacy `pallet-staking` path does not enforce. [5](#0-4)

### Citations

**File:** substrate/frame/staking/src/pallet/mod.rs (L1342-1357)
```rust
			// Only check limits if they are not already a validator.
			if !Validators::<T>::contains_key(stash) {
				// If this error is reached, we need to adjust the `MinValidatorBond` and start
				// calling `chill_other`. Until then, we explicitly block new validators to protect
				// the runtime.
				if let Some(max_validators) = MaxValidatorsCount::<T>::get() {
					ensure!(
						Validators::<T>::count() < max_validators,
						Error::<T>::TooManyValidators
					);
				}
			}

			Self::do_remove_nominator(stash);
			Self::do_add_validator(stash, prefs.clone());
			Self::deposit_event(Event::<T>::ValidatorPrefsSet { stash: ledger.stash, prefs });
```

**File:** substrate/frame/staking/src/pallet/mod.rs (L1374-1431)
```rust
		pub fn nominate(
			origin: OriginFor<T>,
			targets: Vec<AccountIdLookupOf<T>>,
		) -> DispatchResult {
			let controller = ensure_signed(origin)?;

			let ledger = Self::ledger(StakingAccount::Controller(controller.clone()))?;

			ensure!(ledger.active >= MinNominatorBond::<T>::get(), Error::<T>::InsufficientBond);
			let stash = &ledger.stash;

			// Only check limits if they are not already a nominator.
			if !Nominators::<T>::contains_key(stash) {
				// If this error is reached, we need to adjust the `MinNominatorBond` and start
				// calling `chill_other`. Until then, we explicitly block new nominators to protect
				// the runtime.
				if let Some(max_nominators) = MaxNominatorsCount::<T>::get() {
					ensure!(
						Nominators::<T>::count() < max_nominators,
						Error::<T>::TooManyNominators
					);
				}
			}

			ensure!(!targets.is_empty(), Error::<T>::EmptyTargets);
			ensure!(
				targets.len() <= T::NominationsQuota::get_quota(ledger.active) as usize,
				Error::<T>::TooManyTargets
			);

			let old = Nominators::<T>::get(stash).map_or_else(Vec::new, |x| x.targets.into_inner());

			let targets: BoundedVec<_, _> = targets
				.into_iter()
				.map(|t| T::Lookup::lookup(t).map_err(DispatchError::from))
				.map(|n| {
					n.and_then(|n| {
						if old.contains(&n) || !Validators::<T>::get(&n).blocked {
							Ok(n)
						} else {
							Err(Error::<T>::BadTarget.into())
						}
					})
				})
				.collect::<Result<Vec<_>, _>>()?
				.try_into()
				.map_err(|_| Error::<T>::TooManyNominators)?;

			let nominations = Nominations {
				targets,
				// Initial nominations are considered submitted at era 0. See `Nominations` doc.
				submitted_in: CurrentEra::<T>::get().unwrap_or(0),
				suppressed: false,
			};

			Self::do_remove_validator(stash);
			Self::do_add_nominator(stash, nominations);
			Ok(())
```

**File:** prdoc/stable2509/pr_8436.prdoc (L1-11)
```text
title: 'Fix calling nominate on a validator that doesn’t exist silently succeeds'

doc:
  - audience: Runtime Dev
    description: |
      This PR fixes a bug where calling nominate on a validator that doesn’t exist silently succeeds.
      It also updates all the tests that had an incorrect setup - they were simulating elections with unregistered validators.

crates:
  - name: pallet-staking-async
    bump: major
```

**File:** substrate/frame/staking-async/src/tests/bonding.rs (L1684-1708)
```rust
	#[test]
	fn nominating_non_validators_is_not_ok() {
		ExtBuilder::default().nominate(false).build_and_execute(|| {
			// given existing validators
			assert_eq!(
				<Validators<Test>>::iter().map(|(v, _)| v).collect::<Vec<_>>(),
				vec![31, 21, 11,],
			);

			// .. and no existing nominators
			assert!(<Nominators<T>>::iter().count() == 0);
			// and 1 bonded.
			assert_ok!(Staking::bond(RuntimeOrigin::signed(1), 1000, RewardDestination::Stash));

			// then
			assert_noop!(
				Staking::nominate(RuntimeOrigin::signed(1), vec![41]),
				Error::<Test>::BadTarget
			);
			assert_noop!(
				Staking::nominate(RuntimeOrigin::signed(1), vec![31, 21, 11, 41]),
				Error::<Test>::BadTarget
			);
		});
	}
```
