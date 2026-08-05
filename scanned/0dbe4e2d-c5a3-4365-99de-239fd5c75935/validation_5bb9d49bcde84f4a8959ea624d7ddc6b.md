I found a real analog: `pallet-staking`'s `nominate()` extrinsic does not deduplicate the `targets` list before storing it, unlike the newer `pallet-staking-async` implementation which explicitly does.

### Title
`pallet-staking::nominate` accepts duplicate validator targets, letting a nominator's stake be counted multiple times toward one validator in NPoS elections - (File: `substrate/frame/staking/src/pallet/mod.rs`)

### Summary
The `nominate` extrinsic in the legacy `pallet-staking` module stores the caller-supplied `targets: Vec<AccountIdLookupOf<T>>` into the `Nominations.targets` `BoundedVec` without sorting/deduplicating. An unprivileged, signed nominator can therefore submit `nominate(vec![V, V, V, ...])`, repeating the same validator `V` up to the nomination quota. This is the direct analog of the NFTX `addReceiver()` bug — a list of "receivers"/targets is appended without checking whether the entry already exists.

### Finding Description
`nominate` at [1](#0-0)  validates each target against `old.contains(&n) || !Validators::<T>::get(&n).blocked` but never checks `n` against entries already collected in the *same* call, and never dedups the resulting vector before constructing `Nominations`. Compare this to the newer `pallet-staking-async` implementation of the same call, which explicitly sorts and dedups the input before any further processing: [2](#0-1) .

The resulting `Nominations.targets` (with duplicates preserved) is later read verbatim by `get_npos_voters` and packaged as a `(voter, weight, targets)` triple that is fed directly into the NPoS/Phragmén election as one voter's edge list, with no deduplication at this stage either: [3](#0-2) .

In sequential Phragmén/Phragmms, a voter's edges are the targets they support; when the same target appears multiple times in one voter's edge list, that target effectively receives multiple independent "votes"/edges backed by the same underlying stake, at the same time other real validators lose relative competitive weight. This differs qualitatively from ordinary "duplicate opinions," because it changes the graph topology the solver operates on (more edges to one target from the same source) rather than just recording redundant intent.

### Impact Explanation
Existing guards do not stop this path:
- `nominate`'s check `old.contains(&n) || !Validators::<T>::get(&n).blocked` only validates target eligibility, not uniqueness within the newly submitted `targets` vector.
- `get_npos_voters` (both the legacy and even, if triggered, defensive assumptions) trusts `Nominators::<T>::get` to already be well-formed and does not re-validate for duplicate targets.
- The `DuplicateTarget`/`DuplicateVoter` checks found in `election-provider-multi-block`'s `feasibility_check_page_inner` only validate a **submitted mining solution** (`NposSolution`) against the snapshot — they do not run against the raw `Nominators` storage / snapshot construction itself, so a nominator poisoning their own targets list at `nominate()` time is never rejected by that machinery. [4](#0-3) 

This can distort election outcomes (over-representing a validator's support, potentially at the expense of desired-targets selection and legitimately competing validators), which falls squarely under "runtime bugs that compromise intended behavior" for the election/staking subsystem of a live Substrate-based chain.

### Likelihood Explanation
Any signed account that satisfies `MinNominatorBond` can call `nominate` directly with a crafted, duplicated `targets` vector — no admin, governance, relayer, or validator collusion is required. The bug is trivially reproducible and easy to trigger; the only limiting factor is the nomination quota (`T::NominationsQuota::get_quota(ledger.active)`), which bounds how many duplicate copies of the same target can be inserted, but does not prevent the duplication itself.

### Recommendation
Mirror the `pallet-staking-async` fix: after resolving `AccountIdLookupOf<T>` to `T::AccountId`, `sort()` and `dedup()` the `targets` vector in `pallet-staking::nominate` before the eligibility check and before constructing the `Nominations` struct, ensuring `Nominators::targets` can never contain a duplicate `AccountId`.

### Proof of Concept
1. Bond an account `N` with `active >= MinNominatorBond`.
2. Call `Staking::nominate(RuntimeOrigin::signed(N), vec![V, V, V])` where `V` is a valid unblocked validator, and `3 <= NominationsQuota::get_quota(active)`.
3. Inspect `Nominators::<T>::get(N)`; observe `targets == [V, V, V]` (not deduplicated), unlike the equivalent flow in `pallet-staking-async` (`duplicate_nominations_stripped` test) which collapses this to `[V]`. [5](#0-4) 
4. When the election snapshot is built via `get_npos_voters`, the voter `N`'s edge list still contains `V` three times, which is passed as-is into the NPoS solver as `(N, weight, [V, V, V])`.

Note: I could not directly execute the NPoS solver code path in this analysis to numerically confirm the magnitude of vote-weight inflation from repeated edges to the same target within a single voter's `targets` list — this would require running the `sp-npos-elections` solver/tests with a crafted duplicate-target voter to confirm whether the phragmén/phragmms implementation is itself robust to such an edge list (e.g., by internally deduplicating per-voter edges) or whether it actually double-counts support. If a Devin session with full test-execution access confirms the solver *does* dedupe internally, this finding's impact would be reduced to a wasted-storage/weight issue rather than an election-integrity issue, so this should be validated by actually running an election with a crafted duplicate-edge voter and comparing `Support.total`/`voters` breakdown against the deduped case.

### Citations

**File:** substrate/frame/staking/src/pallet/mod.rs (L1374-1420)
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
```

**File:** substrate/frame/staking-async/src/pallet/mod.rs (L2149-2156)
```rust
			// dedup targets
			let mut targets = targets
				.into_iter()
				.map(|t| T::Lookup::lookup(t).map_err(DispatchError::from))
				.collect::<Result<Vec<_>, _>>()?;
			targets.sort();
			targets.dedup();

```

**File:** substrate/frame/staking/src/pallet/impls.rs (L926-942)
```rust
			if let Some(Nominations { targets, .. }) = <Nominators<T>>::get(&voter) {
				if !targets.is_empty() {
					// Note on lazy nomination quota: we do not check the nomination quota of the
					// voter at this point and accept all the current nominations. The nomination
					// quota is only enforced at `nominate` time.

					let voter = (voter, voter_weight, targets);
					if voters_size_tracker.try_register_voter(&voter, &bounds).is_err() {
						// no more space left for the election result, stop iterating.
						Self::deposit_event(Event::<T>::SnapshotVotersSizeExceeded {
							size: voters_size_tracker.size as u32,
						});
						break;
					}

					all_voters.push(voter);
					nominators_taken.saturating_inc();
```

**File:** substrate/frame/election-provider-multi-block/src/verifier/tests.rs (L217-235)
```rust
	#[test]
	fn prevents_duplicate_target_index() {
		ExtBuilder::mock_signed().pages(1).build_and_execute(|| {
			roll_to_snapshot_created();

			// A bad solution with duplicate targets for a single voter in votes2.
			let faulty_page = TestNposSolution {
				// 50% to 0, and then the rest to 0 again, not valid.
				votes2: vec![(0, [(0, PerU16::from_percent(50))], 0)],
				..Default::default()
			};

			assert_noop!(
				VerifierPallet::feasibility_check_page_inner(faulty_page, 0),
				FeasibilityError::NposElection(
					frame_election_provider_support::Error::DuplicateTarget
				),
			);
		});
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
