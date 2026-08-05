### Title
`nominate` in the classic staking pallet stores duplicate validator targets, unlike its `staking-async` counterpart - ([File: substrate/frame/staking/src/pallet/mod.rs])

### Summary
The `Pallet::nominate` extrinsic in `substrate/frame/staking` accepts a `Vec<AccountIdLookupOf<T>>` of validator targets and stores it into `Nominations.targets` without ever deduplicating the list. [1](#0-0)  The sibling, newer `staking-async` pallet implements the exact same call but explicitly sorts and deduplicates the targets before any further processing. [2](#0-1)  This is the same class of bug as the `Pool.initialize` report: an unbounded/unchecked list is accepted from an untrusted caller and persisted with duplicate entries, creating a mismatch between `targets.len()` (used for quota and weight accounting) and the actual number of distinct validators nominated.

### Finding Description
In `substrate/frame/staking/src/pallet/mod.rs`, the `nominate` call:
1. Ensures the caller is a bonded nominator with sufficient stake. [3](#0-2) 
2. Checks `targets.len() <= T::NominationsQuota::get_quota(...)` using the **raw, non-deduplicated** length. [4](#0-3) 
3. Maps/validates each raw target (with duplicates intact) into a `BoundedVec` and stores it directly as `Nominations.targets`. [5](#0-4) 

No `sort()`/`dedup()` step exists anywhere in this path, so `Nominators::<T>::get(stash).targets` can legitimately contain the same validator id multiple times (e.g., `[11, 11, 11, 21, 31]`), exactly mirroring the `Pool.initialize` bug where `committeeArray` could contain repeated addresses because there was no duplicate check before `committeeArray.push(member)`. [6](#0-5) 

The `staking-async` pallet was hardened against exactly this: it deduplicates targets with `targets.sort(); targets.dedup();` before applying the quota check and persisting the nominations, showing the maintainers recognized and fixed this exact issue in the newer implementation but the fix was not backported to the classic `substrate/frame/staking` pallet still shipped in this codebase. [7](#0-6) 

### Impact Explanation
The quota check `targets.len() <= T::NominationsQuota::get_quota(ledger.active)` in the classic pallet is bypassed in spirit: a nominator can consume their entire nomination quota with fewer distinct validators than intended, while causing on-chain storage (`Nominations.targets`) to record an inflated, duplicated list. Downstream consumers of this list (weight computation for `nominate` using `targets.len()`, and any code that assumes `targets.len()` reflects the number of distinct backed validators) operate on a corrupted invariant — the stored array length no longer equals the true number of distinct nominees, the same discrepancy called out in the original report as leading to downstream problems (e.g., insufficient distinct membership even though the raw count looks sufficient).

### Likelihood Explanation
Any signed, bonded nominator can call `nominate` directly with an attacker-chosen `Vec<AccountIdLookupOf<T>>` containing repeated target account ids — no privileged origin, governance, or malicious peer/validator is required, satisfying the "unprivileged public dispatchable" requirement. The existing tests in `substrate/frame/staking/src/tests.rs` confirm that duplicate lists are accepted by the call and only relied upon being "ignored" by the downstream election-provider's voter-processing logic, not by the pallet's own storage invariant, leaving the raw duplicated `Nominations.targets` value itself uncorrected in storage. [8](#0-7) 

### Recommendation
Mirror the fix already present in `staking-async`: sort and `dedup()` the `targets` vector immediately after `Lookup::lookup` resolution and before the `NominationsQuota` length check and `BoundedVec` conversion in `substrate/frame/staking/src/pallet/mod.rs::nominate`, so the quota check and stored `Nominations.targets` both reflect the true count of distinct validators, consistent with the pattern in `substrate/frame/staking-async/src/pallet/mod.rs`.

### Proof of Concept
1. Bond account `1` with sufficient stake for nomination via `Staking::bond`.
2. Call `Staking::nominate(RuntimeOrigin::signed(1), vec![11, 11, 11, 21, 31])` (as done in the existing test `bond_with_duplicate_vote_should_be_ignored_by_election_provider`). [6](#0-5) 
3. Inspect `Nominators::<T>::get(1).targets` — in the classic pallet this stores the raw 5-length list `[11,11,11,21,31]` (no dedup), whereas the same operation on `staking-async` yields the deduplicated `[11,21,31]`. [9](#0-8)  This demonstrates the missing invariant enforcement identified above.

### Citations

**File:** substrate/frame/staking/src/pallet/mod.rs (L1372-1431)
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

**File:** substrate/frame/staking-async/src/pallet/mod.rs (L2149-2178)
```rust
			// dedup targets
			let mut targets = targets
				.into_iter()
				.map(|t| T::Lookup::lookup(t).map_err(DispatchError::from))
				.collect::<Result<Vec<_>, _>>()?;
			targets.sort();
			targets.dedup();

			ensure!(!targets.is_empty(), Error::<T>::EmptyTargets);
			ensure!(
				targets.len() <= T::NominationsQuota::get_quota(ledger.active) as usize,
				Error::<T>::TooManyTargets
			);

			let old = Nominators::<T>::get(stash).map_or_else(Vec::new, |x| x.targets.into_inner());

			let targets: BoundedVec<_, _> = targets
				.into_iter()
				.map(|n| {
					if old.contains(&n) ||
						(Validators::<T>::contains_key(&n) && !Validators::<T>::get(&n).blocked)
					{
						Ok(n)
					} else {
						Err(Error::<T>::BadTarget.into())
					}
				})
				.collect::<Result<Vec<_>, DispatchError>>()?
				.try_into()
				.map_err(|_| Error::<T>::TooManyNominators)?;
```

**File:** substrate/frame/staking/src/tests.rs (L2240-2287)
```rust
#[test]
fn bond_with_duplicate_vote_should_be_ignored_by_election_provider() {
	ExtBuilder::default()
		.validator_count(2)
		.nominate(false)
		.minimum_validator_count(1)
		.set_stake(31, 1000)
		.build_and_execute(|| {
			// ensure all have equal stake.
			assert_eq!(
				<Validators<Test>>::iter()
					.map(|(v, _)| (v, Staking::ledger(v.into()).unwrap().total))
					.collect::<Vec<_>>(),
				vec![(31, 1000), (21, 1000), (11, 1000)],
			);
			// no nominators shall exist.
			assert!(<Nominators<Test>>::iter().map(|(n, _)| n).collect::<Vec<_>>().is_empty());

			// give the man some money.
			let initial_balance = 1000;
			for i in [1, 2, 3, 4].iter() {
				let _ = asset::set_stakeable_balance::<Test>(&i, initial_balance);
			}

			assert_ok!(Staking::bond(
				RuntimeOrigin::signed(1),
				1000,
				RewardDestination::Account(1)
			));
			assert_ok!(Staking::nominate(RuntimeOrigin::signed(1), vec![11, 11, 11, 21, 31]));

			assert_ok!(Staking::bond(
				RuntimeOrigin::signed(3),
				1000,
				RewardDestination::Account(3)
			));
			assert_ok!(Staking::nominate(RuntimeOrigin::signed(3), vec![21, 31]));

			// winners should be 21 and 31. Otherwise this election is taking duplicates into
			// account.
			let supports = <Test as Config>::ElectionProvider::elect(SINGLE_PAGE).unwrap();
			let expected_supports = vec![
				(21, Support { total: 1800, voters: vec![(21, 1000), (1, 400), (3, 400)] }),
				(31, Support { total: 2200, voters: vec![(31, 1000), (1, 600), (3, 600)] }),
			];
			assert_eq!(supports, to_bounded_supports(expected_supports));
		});
}
```

**File:** substrate/frame/staking-async/src/tests/bonding.rs (L1658-1682)
```rust
	#[test]
	fn duplicate_nominations_stripped() {
		ExtBuilder::default().nominate(false).set_stake(31, 1000).build_and_execute(|| {
			// ensure all have equal stake.
			assert_eq!(
				<Validators<Test>>::iter()
					.map(|(v, _)| (v, Staking::ledger(v.into()).unwrap().total))
					.collect::<Vec<_>>(),
				vec![(31, 1000), (21, 1000), (11, 1000)],
			);

			// no nominators shall exist.
			assert!(<Nominators<T>>::iter().map(|(n, _)| n).collect::<Vec<_>>().is_empty());

			bond_nominator(1, 1000, vec![11, 11, 11, 21, 31]);
			assert_eq!(
				Nominators::<T>::get(1).unwrap(),
				Nominations {
					targets: bounded_vec![11, 21, 31],
					submitted_in: 1,
					suppressed: false
				}
			);
		});
	}
```
