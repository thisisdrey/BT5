Confirmed vulnerability found: `remove_member`/`demote_member` in `pallet-ranked-collective` do not touch the `Voting` storage or the ongoing poll `Tally`, so a removed member's vote weight remains counted in every ongoing poll after they lose membership — the exact analog of the reported "ragequit/ragekick vote survives" bug class.

### Title
Removed/demoted ranked-collective member's vote weight remains counted in ongoing poll tallies - (File: `substrate/frame/ranked-collective/src/lib.rs`)

### Summary
`pallet-ranked-collective` (used by e.g. the Fellowship/Ambassador collectives on Collectives-Westend) records each member's vote as a `VoteRecord` with a rank-derived weight, and folds that weight into a poll's `Tally` (`ayes`/`nays`/`bare_ayes`) at the moment of voting via the `vote` extrinsic. When an account subsequently leaves the collective — by being demoted to below the minimum voting rank or removed entirely via `remove_member`/`do_remove_member_from_rank` — nothing reverses the weight it already contributed to any poll that is still ongoing.

### Finding Description
`do_remove_member_from_rank` only mutates rank-indexing storage and `Members`; it never touches `Voting` or invokes `T::Polls` to adjust an ongoing poll's tally: [1](#0-0) 

The `remove_member` extrinsic dispatches directly to that function with no additional cleanup: [2](#0-1) 

`do_demote_member` has the same gap — it removes from-rank indices and updates `Members`, but does not touch `Voting`/`Tally` for ongoing polls where the member's higher-rank-derived vote weight is now stale: [3](#0-2) 

Contrast this with `pallet-collective`, which explicitly implements `ChangeMembers::change_members_sorted` to strip votes cast by any outgoing member from every open proposal's `ayes`/`nays` list at the moment of removal: [4](#0-3) 

`pallet-ranked-collective` has no equivalent hook. Its only vote-cleanup mechanism is `cleanup_poll`, which only works after a poll has *already completed* (`T::Polls::as_ongoing(poll_index).is_none()` must hold) and merely clears the stale `Voting` map entries — it never re-derives the tally, since by then the tally is frozen as `Completed`: [5](#0-4) 

The historical PR record confirms this class of gap existed and was only partially addressed for indexing corruption (`IdToIndex`/`IndexToId`), not for ongoing-poll tallies: [6](#0-5) 
The regression test added for that fix (`remove_member_cleanup_works`) only asserts on `IdToIndex`/`IndexToId` consistency, not on any tally adjustment for open polls: [7](#0-6) 

This is the direct structural analog to the ragequit/ragekick report: a member casts a `Voted` weighted by their rank on an open poll, then the collective's `RemoveOrigin`/`DemoteOrigin` ejects or demotes them for cause — yet their `ayes`/`nays`/`bare_ayes` contribution keeps counting toward quorum and outcome for every poll that was already open, exactly as the LAO shareholder's `No` vote survives their ragequit.

### Impact Explanation
This pallet backs governance collectives on production chains (e.g. Fellowship and Ambassador collectives on Collectives-Westend, referenced via `pallet_ranked_collective_fellowship_collective`/`pallet_ranked_collective_ambassador_collective` weight files). A member who is expelled for misconduct mid-poll retains full voting influence on that poll's outcome, letting an ejected/demoted actor still swing or block a decision (e.g. block their own removal-related follow-up proposal, or spitefully tank unrelated proposals) even though the whole point of `remove_member`/`demote_member` is to strip their standing immediately. This directly matches "runtime bugs that compromise intended behavior" and "unauthorized execution or origin escalation" of the impact gate (stale privilege exercised past authorization).

### Likelihood Explanation
No malicious peer/validator/relayer or leaked key is required — this is a straightforward call-ordering scenario reachable by any account holding `RemoveOrigin`/`DemoteOrigin` privilege (which itself is normal governance operation, not "admin abuse as the root cause"; the bug is the missing cleanup, not the removal action itself) combined with the removed member simply having voted earlier. Any ranked-collective deployment with concurrently open polls and membership churn will trigger this deterministically.

### Recommendation
On `remove_member`/`do_remove_member_from_rank` and `do_demote_member`, iterate the account's `Voting` entries (or an index thereof) for currently-ongoing polls and, for each, call into `T::Polls::try_access_poll` to subtract the corresponding `VoteRecord` weight from the poll's `Tally` before deleting the `Voting` record — mirroring `pallet-collective::change_members_sorted`'s outgoing-vote purge. Alternatively, gate `remove_member`/`demote_member` such that a member cannot be demoted below the rank required by any poll they've actively voted on until that poll closes, or explicitly document/require callers to run `cleanup_poll`-equivalent tally correction as part of the same extrinsic (atomically, not merely as a follow-up permissionless call after completion).

### Proof of Concept
1. Set up `Club` with `RemoveOrigin`/`DemoteOrigin` = Root (as in test mocks).
2. `add_member` and `promote_member` account `3` up to rank 3; create an ongoing poll of class requiring `MinRankOfClass = 3`.
3. `Club::vote(RuntimeOrigin::signed(3), poll, true)` — this increments `tally.bare_ayes`, `tally.ayes` by `rank_to_votes(3, min_rank)` per: [8](#0-7) 
4. While the poll is still `Ongoing`, call `Club::remove_member(RuntimeOrigin::root(), 3, 3)` (or `demote_member` down below the poll's `MinRankOfClass`).
5. Query `T::Polls`/`Voting::<T, I>::get(&poll, &3)` — the `VoteRecord` and the tally increment from step 3 are still present/counted; the poll's `ayes`/`bare_ayes` are unchanged despite `3` no longer being an eligible or existing member. The poll can close and be `Approved`/`Executed` using vote weight from an account that had already been expelled mid-poll.

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

**File:** substrate/frame/ranked-collective/src/lib.rs (L642-669)
```rust
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
```

**File:** substrate/frame/ranked-collective/src/lib.rs (L678-714)
```rust
		/// Remove votes from the given poll. It must have ended.
		///
		/// - `origin`: Must be `Signed` by any account.
		/// - `poll_index`: Index of a poll which is completed and for which votes continue to
		///   exist.
		/// - `max`: Maximum number of vote items from remove in this call.
		///
		/// Transaction fees are waived if the operation is successful.
		///
		/// Weight `O(max)` (less if there are fewer items to remove than `max`).
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
			if r.unique == 0 {
				// return Err(Error::<T, I>::NoneRemaining)
				return Ok(Pays::Yes.into());
			}
			if let Some(cursor) = r.maybe_cursor {
				VotingCleanup::<T, I>::insert(poll_index, BoundedVec::truncate_from(cursor));
			}
			Ok(PostDispatchInfo {
				actual_weight: Some(T::WeightInfo::cleanup_poll(r.unique)),
				pays_fee: Pays::No,
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
