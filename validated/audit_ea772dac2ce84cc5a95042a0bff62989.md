Based on my investigation, I found a real analog to the reported bug class in `pallet-ranked-collective`.

### Title
Removed ranked-collective members' votes remain counted in poll `Tally`, allowing stale/removed-member votes to reach quorum - (File: `substrate/frame/ranked-collective/src/lib.rs`)

### Summary
`pallet-ranked-collective` (the OpenGov membership pallet used e.g. by the Fellowship) never removes or invalidates a member's already-cast vote from an ongoing poll's `Tally` when that member is subsequently removed via `remove_member`. Unlike `pallet-collective`, which explicitly prunes `ayes`/`nays` for outgoing members in `ChangeMembers::change_members_sorted` [1](#0-0) , `pallet-ranked-collective`'s removal path only touches membership indices, not the `Voting`/`Tally` state.

### Finding Description
`remove_member` calls `Self::do_remove_member_from_rank(&who, rank)` [2](#0-1) , which only removes the account from `IdToIndex`, `IndexToId`, `MemberCount`, and `Members`: [3](#0-2) 

Meanwhile, the `Voting` storage double-map records each member's per-poll vote (`VoteRecord`) keyed by `(poll, AccountId)` [4](#0-3) , and a poll's `Tally` (`bare_ayes`, `ayes`, `nays`) is a cumulative counter accrued when `vote()` is called [5](#0-4) . The `vote()` extrinsic only mutates `tally` when the *same* voter changes their vote (by reading their prior `Voting` entry and subtracting it before adding the new one) [6](#0-5) . There is no code path in `remove_member`/`do_remove_member_from_rank` that walks `Voting` for ongoing polls and reverses the removed member's contribution to `bare_ayes`/`ayes`/`nays`.

As a result, once a member votes, is later removed from the collective (or from a rank), their vote weight remains baked into every ongoing poll's `Tally` — contributing to both `support()` and `approval()` calculations used by referenda to decide `ayes`/`nays`/`support` thresholds [7](#0-6) . The stale `Voting` entry (`Voting::<T,I>::get(&poll, &who)`) also persists and is publicly queryable, showing a non-member as an active voter on the poll — precisely the "misleading data exposed to public view functions" failure mode described in the external report.

Note: `VotingCleanup` storage exists and there's a `prdoc` titled "Ensure to cleanup state in remove_member" (`prdoc/1.5.0/pr_2591.prdoc`), but that cleanup is triggered by `Polls::try_state`/poll completion cleanup logic (`try_access_poll`/`kill` paths after a poll ends), not by member removal itself. I could not find, in this codebase snapshot, any hook wired from `remove_member`/`do_remove_member_from_rank` into `Voting`/`Tally` invalidation for currently ongoing polls.

### Impact Explanation
`pallet-ranked-collective` backs the Fellowship/whitelist-origin governance track that can execute privileged referenda (e.g., `whitelist_call` + track execution), so an inflated or stale `Tally` can let a poll reach `support`/`approval` thresholds using votes from accounts that are no longer members — a false-state acceptance of governance intent that can lead to unauthorized privileged execution.

### Likelihood Explanation
This requires no privileged/malicious actor: any account that is a legitimate member, votes on an open poll, and is later removed through the normal `RemoveOrigin`/`DemoteOrigin` (a routine, expected collective operation, not "admin abuse" as root cause) will trigger this — the vulnerable code path (the missing cleanup) is what causes the miscount, not the removal decision itself. Any poll with a long voting period relative to Fellowship membership churn is exposed.

### Recommendation
When removing a member (or demoting below a rank at which they had voted) in `do_remove_member_from_rank`/`remove_member`, iterate ongoing polls the member voted in (or lazily invalidate on poll completion/`try_access_poll`) and subtract their `VoteRecord` weight from the associated `Tally`, and remove the stale `Voting` entry, mirroring the approach `pallet-collective::change_members_sorted` already uses to prune `ayes`/`nays` for outgoing members.

### Proof of Concept
1. Add member A at rank R to the ranked-collective.
2. Open a poll (e.g., a referenda track using this collective's `Tally`).
3. A calls `vote(poll, true)` — `Tally.bare_ayes`/`ayes` incremented by A's `Votes` weight [8](#0-7) .
4. `RemoveOrigin` calls `remove_member(A, R)` — this only touches `Members`/`IdToIndex`/`IndexToId`/`MemberCount` [9](#0-8) ; `Voting::<T,I>::get(&poll, &A)` still returns `Some(Aye(votes))` and `Tally` is unchanged.
5. Poll resolution reads the `Tally` (`support()`/`approval()`), which still includes A's weight even though A is no longer a member — quorum/support can be reached using a vote that should have been invalidated.

### Citations

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

**File:** substrate/frame/ranked-collective/src/lib.rs (L97-102)
```rust
pub struct Tally<T, I, M: GetMaxVoters> {
	bare_ayes: MemberIndex,
	ayes: Votes,
	nays: Votes,
	dummy: PhantomData<(T, I, M)>,
}
```

**File:** substrate/frame/ranked-collective/src/lib.rs (L128-136)
```rust
	fn ayes(&self, _: ClassOf<T, I>) -> Votes {
		self.bare_ayes
	}
	fn support(&self, class: ClassOf<T, I>) -> Perbill {
		Perbill::from_rational(self.bare_ayes, M::get_max_voters(class))
	}
	fn approval(&self, _: ClassOf<T, I>) -> Perbill {
		Perbill::from_rational(self.ayes, 1.max(self.ayes + self.nays))
	}
```

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
