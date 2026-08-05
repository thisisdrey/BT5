## Finding: `pallet_staking::Pallet::nominate` accepts nomination targets that never registered as a validator

### Title
`Staking::nominate` silently accepts non-existent validators as targets, bypassing the `validate()` registration path - (File: `substrate/frame/staking/src/pallet/mod.rs`)

### Summary
The reported bug is a class of "public delegation entrypoint bypasses the explicit registry-registration call", letting stake/voting power flow to an account that was never onboarded through the intended registration function (`addValidator`), which silently breaks reward accounting for both parties. The direct analog in `pallet-staking` is `Pallet::nominate`, whose target-validation check does not verify that a nomination target ever called `validate()` (the pallet's equivalent of `addValidator`); it only inspects the `blocked` flag of a `ValidatorPrefs` value that is returned as a zeroed default for any unregistered account.

### Finding Description
`Staking::validate` is the intended "registration" call: it inserts the caller into the `Validators` storage map via `Self::do_add_validator(stash, prefs)`. [1](#0-0) 

`Staking::nominate`, however, validates each target with the following predicate:
```rust
if old.contains(&n) || !Validators::<T>::get(&n).blocked {
    Ok(n)
} else {
    Err(Error::<T>::BadTarget.into())
}
``` [2](#0-1) 

`Validators::<T>::get(&n)` is a `StorageMap` read that returns the type's `Default` (`ValidatorPrefs { commission: 0, blocked: false }`) for **any** account that is not present in the map — i.e. for any account that never called `validate()`. Because the guard only checks `!prefs.blocked`, and the default is `blocked: false`, an account `n` that has never registered as a validator passes this check exactly like a real, unblocked validator would. There is no `Validators::<T>::contains_key(&n)` check anywhere in this path, so `Self::do_add_nominator(stash, nominations)` is called with a target that was never registered through `validate()`. [3](#0-2) 

This is confirmed by the project's own fix for the equivalent code path in the newer `pallet-staking-async`, which explicitly describes this exact defect: "calling nominate on a validator that doesn't exist silently succeeds," and states that existing tests had been "simulating elections with unregistered validators" as a result. [4](#0-3) 

Corresponding tests in `pallet-staking-async` show the intended, corrected behavior (`Error::<Test>::BadTarget` when nominating a non-validator `41`): [5](#0-4) 

That fix (`pr_8436`) was scoped only to `pallet-staking-async`; the legacy `pallet-staking` crate in this repository still contains the original faulty `.blocked`-only check with no `contains_key` guard, so nominators can still target unregistered "phantom validators."

### Impact Explanation
This directly mirrors the external report's broken invariant: a public, unprivileged entrypoint (`nominate`, analogous to `VotesUpgradeable::delegate`) lets stake/voting power be attributed to an account that bypassed the dedicated registration call (`validate`, analogous to `addValidator`). Consequences:
- Nominators can unknowingly commit their entire nomination-target quota (bounded by `NominationsQuota`) to accounts that can never be elected, since only entries actually present in `Validators` are considered as election candidates. This wastes their stake weight and, if all listed targets are non-existent, they receive zero staking rewards despite having bonded funds and paid for a `nominate` extrinsic — the same "loss of rewards" outcome flagged in the external report.
- It corrupts the invariant that `Nominators::<T>::targets` only ever reference live validators, an invariant multiple parts of the codebase (and the fixed `pallet-staking-async` tests) rely on for correctness of election/reward computation.

### Likelihood Explanation
Trivial and fully permissionless: any signed, bonded account can call `nominate` with an arbitrary `AccountId` that never called `validate()`. No governance, admin, relayer, or malicious-peer assumption is required — this is a straightforward misuse of a public dispatchable with a logic flaw in its own guard clause. The bug is deterministic (not front-run/timing dependent) and reproducible on every block.

### Recommendation
Add an explicit existence check before accepting a nomination target, e.g.:
```rust
if old.contains(&n) || (Validators::<T>::contains_key(&n) && !Validators::<T>::get(&n).blocked) {
    Ok(n)
} else {
    Err(Error::<T>::BadTarget.into())
}
```
This mirrors the fix already shipped for `pallet-staking-async` (`pr_8436`) and should be backported to the legacy `pallet-staking` crate.

### Proof of Concept
1. Bond an account `1` with `Staking::bond(...)`.
2. Call `Staking::nominate(RuntimeOrigin::signed(1), vec![99])` where account `99` has never called `Staking::validate(...)`.
3. Observe the call succeeds (`Ok(())`) and `Nominators::<T>::get(1)` now contains target `99`, even though `Validators::<T>::contains_key(&99)` is `false` — reproducing the same class of "delegate a non-validator" scenario as the external report's PoC, adapted to the staking pallet's `nominate`/`validate` pair instead of `delegate`/`addValidator`. [6](#0-5)

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

**File:** substrate/frame/staking/src/pallet/mod.rs (L1372-1420)
```rust
		#[pallet::call_index(5)]
		#[pallet::weight(T::WeightInfo::nominate(targets.len() as u32))]
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
```

**File:** substrate/frame/staking/src/pallet/mod.rs (L1422-1431)
```rust
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
