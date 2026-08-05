Found the analog. `do_remove_member_from_rank` (full removal, used by `remove_member` extrinsic) was already fixed per [1](#0-0)  to clean up membership index state, but this cleanup is only structural (`IdToIndex`/`IndexToId`/`Members`) — it does **not** touch `Voting<T, I>` storage for any ongoing polls the member has already voted in. Critically, `do_demote_member`, the function backing the `demote_member` extrinsic, has the exact same gap: it calls `Self::remove_from_rank` and then either removes or downgrades the `Members` record, but never removes or re-weights the account's existing `Voting` entries for ongoing polls. [2](#0-1) 

### Title
Demoted/removed ranked-collective member's stale `Voting` entry keeps its original (now-unearned) vote weight in ongoing polls - (File: substrate/frame/ranked-collective/src/lib.rs)

### Summary
`demote_member`/`do_demote_member` and `remove_member`/`do_remove_member_from_rank` update the member index tables and the `Members` rank record, but they never touch the `Voting<T, I>` map. If a member has already cast a vote on an ongoing poll with `vote()`, that vote's recorded `VoteRecord` (which encodes the weighted vote amount tied to the *previous* rank) remains counted in the poll's `Tally` even after the member has been demoted below the poll's `MinRankOfClass` or removed entirely. This mirrors the Hats Protocol `detachHSG()` bug: a state-changing "membership demotion/removal" operation fails to purge previously-granted, now-stale voting rights recorded elsewhere, letting an account retain influence (its old vote weight) it no longer qualifies for.

### Finding Description
`vote()` computes vote weight from the member's rank at cast time via `rank_to_votes` and stores a `VoteRecord` in `Voting::<T, I>` plus accrues it into the poll's live `Tally`: [3](#0-2) .

`do_demote_member` reduces the member's rank (or removes them) but only calls `remove_from_rank`, which solely manipulates `MemberCount`/`IdToIndex`/`IndexToId`: [4](#0-3) . It never iterates `Voting::<T, I>` for that account to subtract or invalidate their prior vote contribution to any still-ongoing poll's `Tally`.

The only mechanism that removes `Voting` entries is `cleanup_poll`, and it is explicitly gated to run only after the poll has ended (`ensure!(T::Polls::as_ongoing(poll_index).is_none(), ...)`): [5](#0-4) . There is no equivalent to `pallet-collective`'s `change_members_sorted`, which explicitly walks all `Proposals` and strips outgoing members' `ayes`/`nays` at membership-change time: [6](#0-5) . `pallet-ranked-collective` has no analogous "strip stale votes from active tallies" step wired into `do_demote_member` or `do_remove_member_from_rank`, even though `pr_2591` demonstrates the maintainers recognized and partially addressed "cleanup state on remove" for the index tables only.

### Impact Explanation
A `Tally` (used to gate `Referenda`/OpenGov-style origin checks for the ranked collective, e.g. Fellowship-driven execution) can retain vote weight from an account that has since been demoted below the poll's required rank or fully removed from the collective. Since referenda thresholds/approval are computed directly off `Tally.ayes`/`Tally.nays`, this can let an ongoing poll incorrectly pass (or fail) based on stale privileges — a runtime bug compromising intended governance/approval behavior, potentially enabling unauthorized execution of a privileged call gated by that poll's origin.

### Likelihood Explanation
This requires no admin/governance abuse and no front-running: any account that (a) is a ranked-collective member, (b) casts a vote on an ongoing poll, and (c) is later demoted/removed through the normal `RemoveOrigin`/`DemoteOrigin` flow (a routine, expected operational action, not an attack) will trigger this stale-state condition. The bug is deterministic and reproducible in every case where a poll remains ongoing across a demotion/removal — no race condition needed, since it's the demotion order (not front-running) that leaves the tally stale for the poll's entire remaining lifetime.

### Recommendation
When demoting (`do_demote_member`) or removing (`do_remove_member_from_rank`) a member, iterate all polls the account currently has a `Voting` record for, and for any poll that is still `Ongoing` (via `T::Polls::try_access_poll`), subtract the member's previously counted `ayes`/`nays`/`bare_ayes` contribution from the live `Tally` and remove (or re-weight) the `Voting` entry — mirroring `pallet-collective::change_members_sorted`'s eager stale-vote pruning, rather than deferring cleanup to `cleanup_poll` which only runs post-completion.

### Proof of Concept
1. `Club::add_member` + `promote_member` account `A` to rank 3; poll `P` opens requiring `MinRankOfClass = 2`.
2. `A` calls `Club::vote(P, true)`; `Tally` for `P` accrues `A`'s rank-3 vote weight; `Voting::<T,I>::get(P, A)` stores the `Aye(votes)` record.
3. `RemoveOrigin`/`DemoteOrigin` calls `Club::demote_member(A)` three times (or `Club::remove_member(A, 3)`) while `P` is still ongoing.
4. `Voting::<T,I>::get(P, A)` is untouched; `P`'s `Tally.ayes` still includes `A`'s original rank-3 weight even though `A` is now rank 0 or not a member at all — the poll can pass/fail based on a weight `A` is no longer entitled to cast.
5. Only after `P` completes can anyone call `cleanup_poll` to remove the stale `Voting` row — too late to affect the already-computed `Tally` outcome.

### Citations

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

**File:** substrate/frame/ranked-collective/src/lib.rs (L649-668)
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
```

**File:** substrate/frame/ranked-collective/src/lib.rs (L690-702)
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
