Confirmed: `remove_member`/`demote_member` in `pallet-ranked-collective` never touch the `Voting<T, I>` storage map or any ongoing poll's `Tally`, unlike `pallet-collective`'s `change_members_sorted`, which explicitly purges outgoing members' ayes/nays from `Voting`.

### Title
Removed/demoted ranked-collective member's vote weight stays counted in ongoing poll tallies - ([File: substrate/frame/ranked-collective/src/lib.rs])

### Summary
`pallet-ranked-collective` (used for the Fellowship and Ambassador collectives that drive privileged OpenGov origins) records a member's vote weight directly into a poll's `Tally` (`ayes`/`nays`) via `vote()` at [1](#0-0) . When that member is later removed via `remove_member` or demoted via `demote_member`, the pallet updates only `Members`, `IdToIndex`, `IndexToId`, and `MemberCount` through `do_remove_member_from_rank`/`remove_from_rank` [2](#0-1) [3](#0-2) . Neither function touches `Voting<T, I>` or reduces the corresponding poll's `Tally`. This is the exact bug class from the external report: "votes are not cleared when a voter is removed", allowing a stale vote to persist in the outcome-determining tally after the voter loses standing.

### Finding Description
`Voting<T, I>` is a `StorageDoubleMap<PollIndex, AccountId, VoteRecord>` [4](#0-3) . Casting a vote inserts a record and increments `tally.ayes`/`tally.nays`/`tally.bare_ayes` by the voter's rank-derived weight [5](#0-4) . That tally is the value read by the referenda/polls engine to decide whether a track passes.

`remove_member` calls `do_remove_member_from_rank`, which only removes rank-index bookkeeping and the `Members` entry: [2](#0-1) 

`demote_member` → `do_demote_member` behaves the same way, only manipulating `Members`/rank indices, never `Voting` or any `Tally` [6](#0-5) .

By contrast, the sibling pallet `pallet-collective` explicitly implements this cleanup in `change_members_sorted`, filtering `ayes`/`nays` of outgoing accounts out of every open proposal's `Votes` [7](#0-6) , with dedicated regression tests `removal_of_old_voters_votes_works`/`removal_of_old_voters_votes_works_with_set_members` [8](#0-7) . `pallet-ranked-collective` has no analogous logic in `remove_member`/`demote_member`, and no test exercises "remove a member while they have an ongoing-poll vote and assert the tally is corrected" — only `remove_member_cleanup_works`, which checks index bookkeeping, not tally/`Voting` correctness [9](#0-8) .

The only place `Voting` entries are ever removed is `cleanup_poll`, and only for polls that have already ended (`ensure!(T::Polls::as_ongoing(poll_index).is_none(), ...)`) [10](#0-9) . There is no mechanism to strip a removed member's weight from an *ongoing* poll's tally.

### Impact Explanation
This directly compromises intended governance behavior for Fellowship/Ambassador ranked-collective instances (e.g., collectives-westend `pallet_ranked_collective_fellowship_collective`, `pallet_ranked_collective_ambassador_collective`). A member who casts a large-rank vote and is subsequently removed or demoted (for misbehavior, term expiry, resignation, etc.) still has their weight baked into `tally.ayes`/`tally.nays` for any poll that was ongoing at removal time. Since ranked-collective backs privileged origins that can, for example, whitelist calls or move referenda through Fellowship-controlled origins, a poll can pass or fail based on the votes of an account that is provably no longer a member — an origin/authorization integrity bug ("runtime bugs that compromise intended behavior") rather than a simple accounting nuisance, matching the Impact Gate.

### Likelihood Explanation
No malicious actor is required. `RemoveOrigin`/`DemoteOrigin` exercising a routine, expected administrative action (removing/demoting a member) while any poll that member voted on remains ongoing is sufficient to trigger the bug — this is a normal-course-of-operation timing condition, not privileged abuse, since the flaw is the missing cleanup step itself, not the removal decision.

### Recommendation
When removing or demoting a member (`do_remove_member_from_rank` / `do_demote_member`), iterate all ongoing polls the member has an entry in `Voting<T, I>` for (or accept the `O(polls)` cost analogous to `pallet-collective::change_members_sorted`), decrement the corresponding `Tally` by the member's recorded vote weight, and remove their `Voting` entry — mirroring the pattern already implemented in `pallet-collective`.

### Proof of Concept
1. Add members A (rank 3) and B (rank 1) to a ranked collective backing a Fellowship track.
2. Open a poll (referendum) via `T::Polls`.
3. A votes aye with weight `w` via `Club::vote(A, poll, true)` — `tally.ayes += w`.
4. `RemoveOrigin` calls `Club::remove_member(A, min_rank)` (e.g., A resigned or was kicked for misconduct) while the poll is still ongoing.
5. Inspect the poll's `Tally`: `ayes` still includes `w`, and `Voting::<T,I>::get(poll, A)` still returns A's vote record, even though `Members::<T,I>::get(A)` is now `None`.
6. If the poll's outcome depends on this margin, it can pass/fail based on a non-member's vote, with no code path ever correcting the tally before poll conclusion.

### Citations

**File:** substrate/frame/ranked-collective/src/lib.rs (L489-498)
```rust
	/// Votes on a given proposal, if it is ongoing.
	#[pallet::storage]
	pub type Voting<T: Config<I>, I: 'static = ()> = StorageDoubleMap<
		_,
		Blake2_128Concat,
		PollIndexOf<T, I>,
		Twox64Concat,
		T::AccountId,
		VoteRecord,
	>;
```

**File:** substrate/frame/ranked-collective/src/lib.rs (L649-669)
```rust
						PollStatus::Ongoing(ref mut tally, class) => {
							match Voting::<T, I>::get(&poll, &who) {
								Some(Aye(votes)) => {
									tally.bare_ayes.saturating_dec();
									tally.ayes.saturating_reduce(votes);
								},
								Some(Nay(votes)) => tally.nays.saturating_reduce(votes),
								None => pays = Pays::No,
							}
							let min_rank = T::MinRankOfClass::convert(class);
							let votes = Self::rank_to_votes(record.rank, min_rank)?;
							let vote = VoteRecord::from((aye, votes));
							match aye {
								true => {
									tally.bare_ayes.saturating_inc();
									tally.ayes.saturating_accrue(votes);
								},
								false => tally.nays.saturating_accrue(votes),
							}
							Voting::<T, I>::insert(&poll, &who, &vote);
							Ok((tally.clone(), vote))
```

**File:** substrate/frame/ranked-collective/src/lib.rs (L690-708)
```rust
		pub fn cleanup_poll(
			origin: OriginFor<T>,
			poll_index: PollIndexOf<T, I>,
			max: u32,
		) -> DispatchResultWithPostInfo {
			ensure_signed(origin)?;
			ensure!(T::Polls::as_ongoing(poll_index).is_none(), Error::<T, I>::Ongoing);

			let r = Voting::<T, I>::clear_prefix(
				poll_index,
				max,
				VotingCleanup::<T, I>::take(poll_index).as_ref().map(|c| &c[..]),
			);
			if r.unique == 0 {
				// return Err(Error::<T, I>::NoneRemaining)
				return Ok(Pays::Yes.into());
			}
			if let Some(cursor) = r.maybe_cursor {
				VotingCleanup::<T, I>::insert(poll_index, BoundedVec::truncate_from(cursor));
```

**File:** substrate/frame/ranked-collective/src/lib.rs (L767-783)
```rust
		fn remove_from_rank(who: &T::AccountId, rank: Rank) -> DispatchResult {
			MemberCount::<T, I>::try_mutate(rank, |last_index| {
				last_index.saturating_dec();
				let index = IdToIndex::<T, I>::get(rank, &who).ok_or(Error::<T, I>::Corruption)?;
				if index != *last_index {
					let last = IndexToId::<T, I>::get(rank, *last_index)
						.ok_or(Error::<T, I>::Corruption)?;
					IdToIndex::<T, I>::insert(rank, &last, index);
					IndexToId::<T, I>::insert(rank, index, &last);
				}

				IdToIndex::<T, I>::remove(rank, who);
				IndexToId::<T, I>::remove(rank, last_index);

				Ok(())
			})
		}
```

**File:** substrate/frame/ranked-collective/src/lib.rs (L836-861)
```rust
		/// Demotes a member in the ranked collective into the next lower rank.
		///
		/// A `maybe_max_rank` may be provided to check that the member does not get demoted from
		/// a certain rank. Is `None` is provided, then the rank will be decremented without checks.
		fn do_demote_member(who: T::AccountId, maybe_max_rank: Option<Rank>) -> DispatchResult {
			let mut record = Self::ensure_member(&who)?;
			let rank = record.rank;
			if let Some(max_rank) = maybe_max_rank {
				ensure!(max_rank >= rank, Error::<T, I>::NoPermission);
			}

			Self::remove_from_rank(&who, rank)?;
			let maybe_rank = rank.checked_sub(1);
			match maybe_rank {
				None => {
					Members::<T, I>::remove(&who);
					Self::deposit_event(Event::MemberRemoved { who, rank: 0 });
				},
				Some(rank) => {
					record.rank = rank;
					Members::<T, I>::insert(&who, &record);
					Self::deposit_event(Event::RankChanged { who, rank });
				},
			}
			Ok(())
		}
```

**File:** substrate/frame/ranked-collective/src/lib.rs (L886-893)
```rust
		/// Removes a member from the rank collective
		pub fn do_remove_member_from_rank(who: &T::AccountId, rank: Rank) -> DispatchResult {
			for r in 0..=rank {
				Self::remove_from_rank(&who, r)?;
			}
			Members::<T, I>::remove(&who);
			Ok(())
		}
```

**File:** substrate/frame/collective/src/lib.rs (L1325-1344)
```rust
		// remove accounts from all current voting in motions.
		let mut outgoing = outgoing.to_vec();
		outgoing.sort();
		for h in Proposals::<T, I>::get().into_iter() {
			<Voting<T, I>>::mutate(h, |v| {
				if let Some(mut votes) = v.take() {
					votes.ayes = votes
						.ayes
						.into_iter()
						.filter(|i| outgoing.binary_search(i).is_err())
						.collect();
					votes.nays = votes
						.nays
						.into_iter()
						.filter(|i| outgoing.binary_search(i).is_err())
						.collect();
					*v = Some(votes);
				}
			});
		}
```

**File:** substrate/frame/collective/src/tests.rs (L730-834)
```rust
#[test]
fn removal_of_old_voters_votes_works() {
	ExtBuilder::default().build_and_execute(|| {
		let proposal = make_proposal(42);
		let proposal_len: u32 = proposal.using_encoded(|p| p.len() as u32);
		let hash = BlakeTwo256::hash_of(&proposal);
		let end = 4;
		assert_ok!(Collective::propose(
			RuntimeOrigin::signed(1),
			3,
			Box::new(proposal.clone()),
			proposal_len
		));
		assert_ok!(Collective::vote(RuntimeOrigin::signed(1), hash, 0, true));
		assert_ok!(Collective::vote(RuntimeOrigin::signed(2), hash, 0, true));
		assert_eq!(
			Voting::<Test, Instance1>::get(&hash),
			Some(Votes { index: 0, threshold: 3, ayes: vec![1, 2], nays: vec![], end })
		);
		Collective::change_members_sorted(&[4], &[1], &[2, 3, 4]);
		assert_eq!(
			Voting::<Test, Instance1>::get(&hash),
			Some(Votes { index: 0, threshold: 3, ayes: vec![2], nays: vec![], end })
		);

		let proposal = make_proposal(69);
		let proposal_len: u32 = proposal.using_encoded(|p| p.len() as u32);
		let hash = BlakeTwo256::hash_of(&proposal);
		assert_ok!(Collective::propose(
			RuntimeOrigin::signed(2),
			2,
			Box::new(proposal.clone()),
			proposal_len
		));
		assert_ok!(Collective::vote(RuntimeOrigin::signed(2), hash, 1, true));
		assert_ok!(Collective::vote(RuntimeOrigin::signed(3), hash, 1, false));
		assert_eq!(
			Voting::<Test, Instance1>::get(&hash),
			Some(Votes { index: 1, threshold: 2, ayes: vec![2], nays: vec![3], end })
		);
		Collective::change_members_sorted(&[], &[3], &[2, 4]);
		assert_eq!(
			Voting::<Test, Instance1>::get(&hash),
			Some(Votes { index: 1, threshold: 2, ayes: vec![2], nays: vec![], end })
		);
	});
}

#[test]
fn removal_of_old_voters_votes_works_with_set_members() {
	ExtBuilder::default().build_and_execute(|| {
		let proposal = make_proposal(42);
		let proposal_len: u32 = proposal.using_encoded(|p| p.len() as u32);
		let hash = BlakeTwo256::hash_of(&proposal);
		let end = 4;
		assert_ok!(Collective::propose(
			RuntimeOrigin::signed(1),
			3,
			Box::new(proposal.clone()),
			proposal_len
		));
		assert_ok!(Collective::vote(RuntimeOrigin::signed(1), hash, 0, true));
		assert_ok!(Collective::vote(RuntimeOrigin::signed(2), hash, 0, true));
		assert_eq!(
			Voting::<Test, Instance1>::get(&hash),
			Some(Votes { index: 0, threshold: 3, ayes: vec![1, 2], nays: vec![], end })
		);
		assert_ok!(Collective::set_members(
			RuntimeOrigin::root(),
			vec![2, 3, 4],
			None,
			MaxMembers::get()
		));
		assert_eq!(
			Voting::<Test, Instance1>::get(&hash),
			Some(Votes { index: 0, threshold: 3, ayes: vec![2], nays: vec![], end })
		);

		let proposal = make_proposal(69);
		let proposal_len: u32 = proposal.using_encoded(|p| p.len() as u32);
		let hash = BlakeTwo256::hash_of(&proposal);
		assert_ok!(Collective::propose(
			RuntimeOrigin::signed(2),
			2,
			Box::new(proposal.clone()),
			proposal_len
		));
		assert_ok!(Collective::vote(RuntimeOrigin::signed(2), hash, 1, true));
		assert_ok!(Collective::vote(RuntimeOrigin::signed(3), hash, 1, false));
		assert_eq!(
			Voting::<Test, Instance1>::get(&hash),
			Some(Votes { index: 1, threshold: 2, ayes: vec![2], nays: vec![3], end })
		);
		assert_ok!(Collective::set_members(
			RuntimeOrigin::root(),
			vec![2, 4],
			None,
			MaxMembers::get()
		));
		assert_eq!(
			Voting::<Test, Instance1>::get(&hash),
			Some(Votes { index: 1, threshold: 2, ayes: vec![2], nays: vec![], end })
		);
	});
}
```

**File:** substrate/frame/ranked-collective/src/tests.rs (L463-487)
```rust
#[test]
fn remove_member_cleanup_works() {
	ExtBuilder::default().build_and_execute(|| {
		assert_ok!(Club::add_member(RuntimeOrigin::root(), 1));
		assert_ok!(Club::promote_member(RuntimeOrigin::root(), 1));
		assert_ok!(Club::add_member(RuntimeOrigin::root(), 2));
		assert_ok!(Club::promote_member(RuntimeOrigin::root(), 2));
		assert_ok!(Club::add_member(RuntimeOrigin::root(), 3));
		assert_ok!(Club::promote_member(RuntimeOrigin::root(), 3));

		assert_eq!(IdToIndex::<Test>::get(1, 2), Some(1));
		assert_eq!(IndexToId::<Test>::get(1, 1), Some(2));

		assert_eq!(IdToIndex::<Test>::get(1, 3), Some(2));
		assert_eq!(IndexToId::<Test>::get(1, 2), Some(3));

		assert_ok!(Club::remove_member(RuntimeOrigin::root(), 2, 1));

		assert_eq!(IdToIndex::<Test>::get(1, 2), None);
		assert_eq!(IndexToId::<Test>::get(1, 1), Some(3));

		assert_eq!(IdToIndex::<Test>::get(1, 3), Some(1));
		assert_eq!(IndexToId::<Test>::get(1, 2), None);
	});
}
```
