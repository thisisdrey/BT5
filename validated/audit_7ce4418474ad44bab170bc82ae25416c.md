### Title
Removed or demoted ranked-collective members' votes remain counted in ongoing poll tallies - (File: `substrate/frame/ranked-collective/src/lib.rs`)

### Summary
`pallet-ranked-collective` records a member's vote weight directly into a poll's `Tally` (`bare_ayes`, `ayes`, `nays`) at `vote()` time [1](#0-0) . When that member is later removed (`remove_member`) or demoted below the poll's minimum rank (`demote_member`), the pallet only mutates the rank-indexing storage (`MemberCount`, `IdToIndex`, `IndexToId`, `Members`) and never touches the `Voting` map or the already-accrued `Tally` for any currently **ongoing** poll [2](#0-1) [3](#0-2) . This is the same broken invariant described in the oDAO report: a vote cast by an account is still counted in the aggregate result after that account has ceased to be an eligible voter.

### Finding Description
`vote()` inserts the caller's `VoteRecord` into `Voting::<T, I>` and simultaneously accrues the member's rank-derived vote weight straight into the poll's live `TallyOf<T, I>` structure held by `T::Polls` (e.g. `pallet-referenda`'s `ReferendumInfo`) [4](#0-3) .

`do_remove_member_from_rank`/`remove_from_rank` and `do_demote_member` update only the rank bookkeeping (`MemberCount`, `IdToIndex`, `IndexToId`, `Members`) [5](#0-4) [6](#0-5) . Neither function iterates open polls to strip the removed/demoted account's contribution from the `Tally`, and `Voting` entries for that account on ongoing polls are left untouched.

The only mechanism that clears `Voting` entries is `cleanup_poll`, and it explicitly refuses to run while the poll is still ongoing: `ensure!(T::Polls::as_ongoing(poll_index).is_none(), Error::<T, I>::Ongoing);` [7](#0-6) . So there is no path to remove a stale vote from an ongoing poll's tally once the voter is no longer eligible.

This is inconsistent with how the codebase handles the identical problem elsewhere:
- `pallet-collective::change_members_sorted` explicitly walks every open `Proposals` entry and filters outgoing members out of `ayes`/`nays` at the moment membership changes [8](#0-7) , verified by `removal_of_old_voters_votes_works` [9](#0-8) .
- `pallet-oracle`'s `ChangeMembers::change_members_sorted` clears `RawValues` for outgoing operators [10](#0-9) , verified by `should_clear_data_for_removed_members` [11](#0-10) .

`pallet-ranked-collective` has no equivalent cleanup hook for `remove_member`/`demote_member` touching in-flight polls, even though a prior fix (`pr_2591`, "Ensure to cleanup state in `remove_member`") already addressed *some* state-cleanup gaps for this pallet [12](#0-11) , but not the ongoing-poll `Tally`/`Voting` inconsistency.

### Impact Explanation
`pallet-ranked-collective` backs `pallet-referenda` decision-making for fellowship/technical-committee style governance (e.g. Rococo's Fellowship configuration uses `pallet_ranked_collective::TallyOf` as the referenda `Tally`) [13](#0-12) . A stale, uncorrected `Tally` weight from a removed/demoted member can push a borderline referendum over its approval/support threshold, or keep it above threshold, when the current live electorate would not actually support it. This is a runtime bug that compromises intended governance behavior — an origin-escalation-adjacent outcome where a decision is executed with unauthorized influence baked into it, since the tally used for the pass/fail decision no longer reflects the actual eligible voter set.

### Likelihood Explanation
The bug triggers deterministically any time a member who has already voted on an ongoing poll is subsequently removed (`remove_member`) or demoted below the poll's `MinRankOfClass` (`demote_member`) before the poll concludes. No special timing, race, or malicious infrastructure is required — it is a straightforward state-cleanup omission that reproduces on every run given that sequence of calls.

### Recommendation
When removing or demoting a member (`do_remove_member_from_rank`, `do_demote_member`), iterate the member's `Voting` entries for all currently ongoing polls (mirroring `pallet-collective::change_members_sorted`) and, via `T::Polls::try_access_poll`, subtract the member's previously-accrued `ayes`/`nays`/`bare_ayes` weight from the live `Tally` before removing the `Voting` record. Alternatively, disallow rank changes/removals for members with a `Voting` entry on any still-ongoing poll until their vote is retracted.

### Proof of Concept
1. Configure a poll with `MinRankOfClass` such that rank-2 member `A` can vote with weight `W`.
2. `A` calls `Club::vote(origin(A), poll, true)` — the poll's `Tally.ayes` increases by `W` and `Voting::<T,I>::get(poll, A)` is set to `Aye(W)` [1](#0-0) .
3. `RemoveOrigin` (or `DemoteOrigin`) removes/demotes `A` below the poll's minimum rank via `remove_member`/`demote_member` [2](#0-1) [6](#0-5) .
4. The poll's `Tally.ayes` is unchanged — still includes `W` from `A`, and `Voting::<T,I>::get(poll, A)` still returns `Aye(W)`.
5. `cleanup_poll` cannot be called to strip this vote because the poll is still `Ongoing` (`Error::Ongoing`) [7](#0-6) .
6. The poll closes/decides using a `Tally` that still counts `A`'s now-invalid vote weight, potentially flipping the outcome versus what the actual eligible electorate would produce.

### Citations

**File:** substrate/frame/ranked-collective/src/lib.rs (L600-617)
```rust
		pub fn remove_member(
			origin: OriginFor<T>,
			who: AccountIdLookupOf<T>,
			min_rank: Rank,
		) -> DispatchResultWithPostInfo {
			let max_rank = T::RemoveOrigin::ensure_origin(origin)?;
			let who = T::Lookup::lookup(who)?;
			let MemberRecord { rank, .. } = Self::ensure_member(&who)?;
			ensure!(min_rank >= rank, Error::<T, I>::InvalidWitness);
			ensure!(max_rank >= rank, Error::<T, I>::NoPermission);

			Self::do_remove_member_from_rank(&who, rank)?;
			Self::deposit_event(Event::MemberRemoved { who, rank });
			Ok(PostDispatchInfo {
				actual_weight: Some(T::WeightInfo::remove_member(rank as u32)),
				pays_fee: Pays::Yes,
			})
		}
```

**File:** substrate/frame/ranked-collective/src/lib.rs (L630-676)
```rust
		#[pallet::call_index(4)]
		#[pallet::weight(T::WeightInfo::vote())]
		pub fn vote(
			origin: OriginFor<T>,
			poll: PollIndexOf<T, I>,
			aye: bool,
		) -> DispatchResultWithPostInfo {
			let who = ensure_signed(origin)?;
			let record = Self::ensure_member(&who)?;
			use VoteRecord::*;
			let mut pays = Pays::Yes;

			let (tally, vote) = T::Polls::try_access_poll(
				poll,
				|mut status| -> Result<(TallyOf<T, I>, VoteRecord), DispatchError> {
					match status {
						PollStatus::None | PollStatus::Completed(..) => {
							Err(Error::<T, I>::NotPolling)?
						},
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
						},
					}
				},
			)?;
			Self::deposit_event(Event::Voted { who, poll, vote, tally });
			Ok(pays.into())
		}
```

**File:** substrate/frame/ranked-collective/src/lib.rs (L688-702)
```rust
		#[pallet::call_index(5)]
		#[pallet::weight(T::WeightInfo::cleanup_poll(*max))]
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

**File:** substrate/frame/collective/src/lib.rs (L1312-1347)
```rust
	fn change_members_sorted(
		_incoming: &[T::AccountId],
		outgoing: &[T::AccountId],
		new: &[T::AccountId],
	) {
		if new.len() > T::MaxMembers::get() as usize {
			log::error!(
				target: LOG_TARGET,
				"New members count ({}) exceeds maximum amount of members expected ({}).",
				new.len(),
				T::MaxMembers::get(),
			);
		}
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
		Members::<T, I>::put(new);
		Prime::<T, I>::kill();
	}
```

**File:** substrate/frame/collective/src/tests.rs (L730-776)
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
```

**File:** substrate/frame/honzon/oracle/src/lib.rs (L432-447)
```rust
impl<T: Config<I>, I: 'static> ChangeMembers<T::AccountId> for Pallet<T, I> {
	fn change_members_sorted(
		_incoming: &[T::AccountId],
		outgoing: &[T::AccountId],
		_new: &[T::AccountId],
	) {
		// remove values
		for removed in outgoing {
			let _ = RawValues::<T, I>::clear_prefix(removed, u32::MAX, None);
		}
	}

	fn set_prime(_prime: Option<T::AccountId>) {
		// nothing
	}
}
```

**File:** substrate/frame/honzon/oracle/src/tests.rs (L298-314)
```rust
#[test]
fn should_clear_data_for_removed_members() {
	new_test_ext().execute_with(|| {
		assert_ok!(ModuleOracle::feed_values(
			RuntimeOrigin::signed(1),
			vec![(50, 1000)].try_into().unwrap()
		));
		assert_ok!(ModuleOracle::feed_values(
			RuntimeOrigin::signed(2),
			vec![(50, 1000)].try_into().unwrap()
		));

		ModuleOracle::change_members_sorted(&[4], &[1], &[2, 3, 4]);

		assert_eq!(ModuleOracle::raw_values(&1, 50), None);
	});
}
```

**File:** prdoc/1.5.0/pr_2591.prdoc (L1-12)
```text
title: Ensure to cleanup state in `remove_member`

author: bkchr
topic: runtime

doc:
  - audience: Runtime Dev
    description: |
      Cleans up the state properly if a member of a ranked collective is removed.

crates:
  - name: pallet-ranked-collective
```

**File:** polkadot/runtime/rococo/src/governance/fellowship.rs (L308-309)
```rust
	type Votes = pallet_ranked_collective::Votes;
	type Tally = pallet_ranked_collective::TallyOf<Runtime, FellowshipCollectiveInstance>;
```
