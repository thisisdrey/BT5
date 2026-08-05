Audit Report

## Title
Removed/demoted ranked-collective member's vote weight remains counted in ongoing poll tallies - (File: `substrate/frame/ranked-collective/src/lib.rs`)

## Summary
`pallet-ranked-collective`'s `do_remove_member_from_rank` and `do_demote_member` only mutate rank-indexing storage (`IdToIndex`/`IndexToId`/`MemberCount`) and `Members`, but never touch the `Voting` map or invoke `T::Polls::try_access_poll` to subtract a departing member's already-cast vote weight from any ongoing poll's `Tally`. As a result, a member's `bare_ayes`/`ayes`/`nays` contribution recorded via the `vote` extrinsic remains fully counted toward quorum and outcome in every poll that was open at the time of their removal or demotion, even though they no longer hold the rank required to have cast that vote.

## Finding Description
The `vote` extrinsic increments `tally.bare_ayes`/`tally.ayes`/`tally.nays` by a rank-derived weight (`rank_to_votes`) and stores a `VoteRecord` in `Voting::<T, I>` for the poll: [1](#0-0) 

`do_remove_member_from_rank`, invoked directly by the `remove_member` extrinsic, only calls `remove_from_rank` for each rank up to the member's rank and removes the `Members` entry — it never reads or mutates `Voting` nor calls into `T::Polls`: [2](#0-1) [3](#0-2) 

`do_demote_member` has the identical gap: it adjusts rank-index storage and updates the member's rank in `Members`, without any corresponding adjustment to `Voting`/`Tally` for polls the member already voted on at their higher, now-stale rank: [4](#0-3) 

The only vote-cleanup path, `cleanup_poll`, explicitly requires the poll to have already completed (`T::Polls::as_ongoing(poll_index).is_none()`) and merely clears stale `Voting` entries without touching any tally, since by then the tally is frozen: [5](#0-4) 

This is structurally different from `pallet-collective`, which implements `ChangeMembers::change_members_sorted` to strip an outgoing member's votes from every open proposal's `ayes`/`nays` at the moment of removal: [6](#0-5) 

`pallet-ranked-collective` has no equivalent mechanism, so the entry point for the exploit is the public, unprivileged `vote` extrinsic (any member can call it), with the bug materializing regardless of who later removes/demotes the member — the tally corruption exists in the pallet's state machine and is exposed the moment a poll closes and is tallied with stale votes still counted.

## Impact Explanation
Any account that casts a weighted vote on an open poll via the public `vote` extrinsic and later loses standing (rank drop below the poll's minimum, or full removal) retains full voting influence on that poll's outcome. The poll's `ayes`/`nays`/`bare_ayes` in `Tally` is never corrected, so `T::Polls::try_access_poll`-driven approval/rejection decisions are computed using vote weight from an account that is no longer entitled to it. This is a runtime bug that compromises the intended behavior of the collective's membership/removal semantics — a stale privilege (previously cast vote weight) is exercised past the point its authorization (rank/membership) was revoked, matching "runtime bugs that compromise intended behavior" in the impact gate.

## Likelihood Explanation
The triggering condition — an account voting via the public `vote` extrinsic on an open poll, and that poll remaining open when the account's rank changes — is deterministic and requires no malicious node, relayer, or leaked key; it is a straightforward ordering issue reachable in any deployment with concurrent open polls and membership churn (e.g. Fellowship/Ambassador collectives). The vote itself is fully public and unprivileged.

## Recommendation
On `do_remove_member_from_rank` and `do_demote_member`, before deleting or reducing the caller's `Voting` record, iterate that account's ongoing-poll votes and call `T::Polls::try_access_poll` to subtract the corresponding `VoteRecord` weight from `Tally.ayes`/`Tally.nays`/`Tally.bare_ayes`, mirroring `pallet-collective::change_members_sorted`'s outgoing-vote purge, so tally correction happens atomically with the rank change rather than only being possible after a poll is already `Completed`.

## Proof of Concept
1. `add_member`/`promote_member` account `3` to rank ≥ `MinRankOfClass`; open a poll of that class.
2. `Club::vote(RuntimeOrigin::signed(3), poll, true)` — increments `tally.bare_ayes`/`tally.ayes` per `rank_to_votes(3, min_rank)` (`substrate/frame/ranked-collective/src/lib.rs` L642-669).
3. While the poll is still `Ongoing`, demote or remove account `3` below `MinRankOfClass` (`do_demote_member`/`do_remove_member_from_rank`, L840-861 / L886-893).
4. Observe `Voting::<T, I>::get(&poll, &3)` and the poll's `Tally` are unchanged — the vote weight from step 2 remains counted despite account `3` no longer holding sufficient rank, letting the poll close and resolve using stale vote weight.

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

**File:** substrate/frame/ranked-collective/src/lib.rs (L690-714)
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
			}
			Ok(PostDispatchInfo {
				actual_weight: Some(T::WeightInfo::cleanup_poll(r.unique)),
				pays_fee: Pays::No,
			})
		}
```

**File:** substrate/frame/ranked-collective/src/lib.rs (L840-861)
```rust
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
