This confirms the mechanism: `voter_weight = weight_of(&voter)` is fetched fresh at the moment each page of the election snapshot is built [1](#0-0) , while the `VoterList` (bags-list) ordering/inclusion is a stale, lazily-maintained cache that is explicitly *not* updated on bonding/unbonding changes [2](#0-1) . I have enough evidence to produce the analog report.

### Title
Temporary stake inflation via `pallet-bags-list::put_in_front_of(_other)` lets an unprivileged staker buy a favorable position in the NPoS voter snapshot with borrowed capital - (File: `substrate/frame/bags-list/src/list/mod.rs`)

### Summary
The external report describes a matching-engine bug where a frontrunner temporarily supplies excess liquidity to jump to the head of a sorted doubly-linked list (`insertSorted`/`updateSuppliers`), gets matched at that favorable position, then withdraws the excess once the benefit is locked in. The Polkadot SDK analog is `pallet-bags-list`, an on-chain "semi-sorted linked list, with permissionless sorting" [3](#0-2) , used by staking as the `VoterList` that feeds the NPoS election snapshot. The pallet's `put_in_front_of`/`put_in_front_of_other` extrinsics allow anyone to reorder a node within a bag purely based on a momentary comparison of `T::ScoreProvider::score()` at call time [4](#0-3) , and the pallet's own docs acknowledge that score changes caused by bonding/unbonding are *not* automatically propagated and must be "manually fixed by the staker" via `rebag`/`putInFrontOf` [2](#0-1) .

### Finding Description
1. A staker can call `bond_extra` to temporarily inflate their stake, making `T::ScoreProvider::score(&heavier_id)` exceed a chosen `lighter_id`'s score.
2. They (or anyone, since `put_in_front_of_other` is explicitly permissionless [5](#0-4) ) call `put_in_front_of`/`put_in_front_of_other`, which checks only the current score ("the most expensive check, so we do it last") and then splices the node directly in front of `lighter_id` [6](#0-5) .
3. This new position is not re-validated later; bag reordering only happens via `rebag`/`on_idle`, and score is otherwise stale until externally corrected [7](#0-6) .
4. During election snapshot generation, the `VoterList` is locked and iterated page by page (`get_npos_voters`), but the actual `voter_weight` used for the election is fetched live via `weight_of_fn` at read time, not from the bags-list's cached score [8](#0-7) .
5. The pallet's own test explicitly documents that stake changes made once a voter is already captured in a completed page — or before their page is read while still ahead in the (now stale) list — are not reflected/re-validated: "51 who is already part of the list might want to unbond... their position is not updated" [9](#0-8) .

Putting these together: an attacker can (a) `bond_extra` to inflate stake, (b) use `put_in_front_of`/`put_in_front_of_other` to jump ahead of a bounded cutoff point in their bag (guaranteeing inclusion in a `DataProviderBounds`-truncated voter page that would otherwise exclude them), (c) have their inflated `voter_weight` captured live once their page of the snapshot is generated, then (d) `unbond` the borrowed capital immediately afterward. Because the bags-list position is only lazily corrected (via permissionless-but-optional `rebag`/`on_idle`), the attacker keeps the benefit of the already-computed election voter weight without holding the capital for the full unbonding/election period — the same non-atomic "borrow position, get matched, withdraw" primitive as the original report, replacing "supply units to a lending pool" with "bond stake to `pallet-staking`" and "matched in p2p" with "captured in the NPoS voter snapshot".

### Impact Explanation
This directly affects "runtime bugs that compromise intended behavior" for a Substrate-based chain: it lets an unprivileged staker distort the composition/weighting of the NPoS election voter set — and therefore validator selection and reward distribution — using capital that is not actually staked for the election's economic security period. This weakens the core security assumption of NPoS (that voting power reflects locked, at-risk stake) without needing a malicious validator, collator, relayer, or governance actor.

### Likelihood Explanation
Medium: it requires the attacker to time `bond_extra` → `put_in_front_of(_other)` → (wait for their bag/page to be processed in the snapshot) → `unbond`, across a window that can span multiple blocks/pages for a multi-page election snapshot. This mirrors the original report's caveat that "the frontrunner needs excessive capital for a block's time period" — here the capital must be held for the shorter of (a page-processing window) rather than for the entire unbonding duration, which meaningfully lowers the capital cost/duration required for the attack.

### Recommendation
- Re-validate voter weight/order consistency at commit time of the election result, or snapshot the `VoterList` score atomically with weight capture rather than reading it live per page.
- Consider disallowing `put_in_front_of`/`put_in_front_of_other` reordering (or bond/unbond-driven score changes) while a `VoterList` `Lock` is held/pending processing for the in-progress snapshot page, closing the timing window between reorder and page capture.
- Consider a minimum bonding duration or "effective stake" that requires stake to have existed for some minimum number of blocks before being counted with full weight in the election snapshot, to prevent momentary capital injection from buying voter weight.

### Proof of Concept
Conceptual sequence, using the pallet's own test primitives:
1. `StakingMock::set_score_of(&attacker, X)` via real `bond_extra(origin, extra)` so `attacker`'s score exceeds `lighter`'s score within the same bag (mirrors `substrate/frame/bags-list/src/tests.rs:301-318` `put_in_front_of_other_can_be_permissionless`).
2. Call `BagsList::put_in_front_of_other(RuntimeOrigin::signed(anyone), attacker, lighter)` — succeeds solely because `ScoreProvider::score(attacker) > ScoreProvider::score(lighter)` at this instant (`substrate/frame/bags-list/src/list/mod.rs:474-478`), placing `attacker` ahead of the bounded voter-page cutoff.
3. Election snapshot processing (`Staking::electing_voters`/`get_npos_voters`) reaches `attacker`'s new position and captures `voter_weight = weight_of(&attacker)` (`substrate/frame/staking-async/src/pallet/impls.rs:885,913`), including the inflated stake in the election.
4. `attacker` immediately calls `unbond(extra)`. As shown by `voter_list_not_updated_when_locked` (`substrate/frame/staking-async/src/tests/election_data_provider.rs:830-843`), the bags-list position/lock state does not retroactively invalidate the already-read voter weight for the in-progress snapshot.
5. The attacker's inflated, no-longer-backed stake has already influenced validator selection/reward weighting for the election period.

### Citations

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L882-913)
```rust
		let mut all_voters = Vec::<_>::with_capacity(page_len_prediction as usize);

		// cache a few things.
		let weight_of = Self::weight_of_fn();

		let mut voters_seen = 0u32;
		let mut validators_taken = 0u32;
		let mut nominators_taken = 0u32;
		let mut min_active_stake = u64::MAX;

		let mut sorted_voters = match status {
			// start the snapshot processing from the beginning.
			SnapshotStatus::Waiting => T::VoterList::iter(),
			// snapshot continues, start from the last iterated voter in the list.
			SnapshotStatus::Ongoing(account_id) => T::VoterList::iter_from(&account_id)
				.defensive_unwrap_or(Box::new(vec![].into_iter())),
			// all voters have been consumed already, return an empty iterator.
			SnapshotStatus::Consumed => Box::new(vec![].into_iter()),
		};

		while all_voters.len() < page_len_prediction as usize &&
			voters_seen < (NPOS_MAX_ITERATIONS_COEFFICIENT * page_len_prediction as u32)
		{
			let voter = match sorted_voters.next() {
				Some(voter) => {
					voters_seen.saturating_inc();
					voter
				},
				None => break,
			};

			let voter_weight = weight_of(&voter);
```

**File:** substrate/frame/staking/src/pallet/mod.rs (L259-271)
```rust
		/// Something that provides a best-effort sorted list of voters aka electing nominators,
		/// used for NPoS election.
		///
		/// The changes to nominators are reported to this. Moreover, each validator's self-vote is
		/// also reported as one independent vote.
		///
		/// To keep the load off the chain as much as possible, changes made to the staked amount
		/// via rewards and slashes are not reported and thus need to be manually fixed by the
		/// staker. In case of `bags-list`, this always means using `rebag` and `putInFrontOf`.
		///
		/// Invariant: what comes out of this list will always be a nominator.
		#[pallet::no_default]
		type VoterList: SortedListProvider<Self::AccountId, Score = VoteWeight>;
```

**File:** substrate/frame/bags-list/src/lib.rs (L29-31)
```rust
//!
//! An onchain implementation of a semi-sorted linked list, with permissionless sorting and update
//! operations.
```

**File:** substrate/frame/bags-list/src/lib.rs (L70-80)
```rust
//! - items are kept in bags, which are delineated by their range of score (See
//!   [`Config::BagThresholds`]).
//! - for iteration, bags are chained together from highest to lowest and elements within the bag
//!   are iterated from head to tail.
//! - items within a bag are iterated in order of insertion. Thus removing an item and re-inserting
//!   it will worsen its position in list iteration; this reduces incentives for some types of spam
//!   that involve consistently removing and inserting for better position. Further, ordering
//!   granularity is thus dictated by range between each bag threshold.
//! - if an item's score changes to a value no longer within the range of its current bag the item's
//!   position will need to be updated by an external actor with rebag (update), or removal and
//!   insertion.
```

**File:** substrate/frame/bags-list/src/list/mod.rs (L463-495)
```rust
	/// Put `heavier_id` to the position directly in front of `lighter_id`. Both ids must be in the
	/// same bag and the `score_of` `lighter_id` must be less than that of `heavier_id`.
	pub(crate) fn put_in_front_of(
		lighter_id: &T::AccountId,
		heavier_id: &T::AccountId,
	) -> Result<(), ListError> {
		let lighter_node = Node::<T, I>::get(&lighter_id).ok_or(ListError::NodeNotFound)?;
		let heavier_node = Node::<T, I>::get(&heavier_id).ok_or(ListError::NodeNotFound)?;

		ensure!(lighter_node.bag_upper == heavier_node.bag_upper, ListError::NotInSameBag);

		// this is the most expensive check, so we do it last.
		ensure!(
			T::ScoreProvider::score(&heavier_id) > T::ScoreProvider::score(&lighter_id),
			ListError::NotHeavier
		);

		// remove the heavier node from this list. Note that this removes the node from storage and
		// decrements the node counter.
		let _ =
			Self::remove(&heavier_id).defensive_proof("both nodes have been checked to exist; qed");

		// re-fetch `lighter_node` from storage since it may have been updated when `heavier_node`
		// was removed.
		let lighter_node =
			Node::<T, I>::get(lighter_id).defensive_ok_or_else(|| ListError::NodeNotFound)?;

		// insert `heavier_node` directly in front of `lighter_node`. This will update both nodes
		// in storage and update the node counter.
		Self::insert_at_unchecked(lighter_node, heavier_node);

		Ok(())
	}
```

**File:** substrate/frame/bags-list/src/tests.rs (L301-318)
```rust
	#[test]
	fn put_in_front_of_other_can_be_permissionless() {
		ExtBuilder::default()
			.skip_genesis_ids()
			.add_ids(vec![(10, 15), (11, 16), (12, 19)])
			.build_and_execute(|| {
				// given
				assert_eq!(List::<Runtime>::get_bags(), vec![(20, vec![10, 11, 12])]);
				// 11 now has more weight than 10 and can be moved before it.
				StakingMock::set_score_of(&11u64, 17);

				// when
				assert_ok!(BagsList::put_in_front_of_other(RuntimeOrigin::signed(42), 11u64, 10));

				// then
				assert_eq!(List::<Runtime>::get_bags(), vec![(20, vec![11, 10, 12])]);
			});
	}
```

**File:** substrate/frame/staking-async/src/tests/election_data_provider.rs (L830-843)
```rust
				assert_eq!(pallet_bags_list::Lock::<T, VoterBagsListInstance>::get(), Some(()));

				// 51 who is already part of the list might want to unbond. They are already in the
				// snapshot, and their position is not updated
				hypothetically!({
					assert_ok!(Staking::unbond(RuntimeOrigin::signed(51), 500));
					// they are still in the original bag
					assert_eq!(
						pallet_bags_list::ListNodes::<T, VoterBagsListInstance>::get(51)
							.unwrap()
							.bag_upper,
						10_000
					);
				});
```
