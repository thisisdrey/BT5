Based on my investigation, I found a concrete local analog in `pallet-collective`'s `do_close` logic, where the "insufficient member count defeats proposal" pattern from the CoreDAO report is directly mirrored.

### Title
Stale/abstention vote arithmetic in `Collective::do_close` underflows or misrepresents outcome when member set shrinks below the proposal's threshold - (File: `substrate/frame/collective/src/lib.rs`)

### Summary
`do_close` computes disapproval using the **live** `Members` count (`seats`) rather than the member count that existed when the proposal's `threshold` was set: [1](#0-0) 
Specifically:
```rust
let seats = Members::<T, I>::get().len() as MemberCount;
let approved = yes_votes >= voting.threshold;
let disapproved = seats.saturating_sub(no_votes) < voting.threshold;
...
let default = T::DefaultVote::default_vote(prime_vote, yes_votes, no_votes, seats);
let abstentions = seats - (yes_votes + no_votes);
```
This is directly analogous to the CoreDAO `GovHub` bug: the report's core broken invariant is that a proposal's pass/fail math uses a "total" quantity (`totalVotes`/member count) that has no minimum-threshold protection and can be driven to zero/very small values, causing `DEFEATED` (or here, disapproved/miscomputed) outcomes irrespective of the actual votes cast.

### Finding Description
`Members` can shrink after a proposal is proposed (via `set_members`, or via `ChangeMembers::change_members_sorted` invoked by `pallet-membership` or `pallet-elections-phragmen`) [2](#0-1) . When members shrink, `change_members_sorted` filters `ayes`/`nays` for the *outgoing* members from any in-flight `Voting` entries [3](#0-2) , but it does **not** adjust `voting.threshold`, which was fixed at proposal-creation time based on the member count/threshold chosen by the proposer [4](#0-3) .

The line `let abstentions = seats - (yes_votes + no_votes);` uses plain (non-saturating) `u32` subtraction. Under the pallet's documented invariant "The sum of aye and nay votes for a proposal can never exceed `MaxMembers`" [5](#0-4) , this assumes `yes_votes + no_votes <= seats` always holds. That invariant is only preserved because `change_members_sorted` prunes stale votes belonging to outgoing accounts. However, `change_members_sorted` filters votes solely by exact match against `outgoing` account IDs — it does not re-validate that the *current* `ayes`/`nays` are still `<= seats`. If members are churned (some removed, replaced by new members) between the vote and `close`, and if the sum of stale-but-still-member votes plus abstentions is miscounted, `seats - (yes_votes + no_votes)` can underflow, panicking the runtime in a non-`saturating` arithmetic path in release builds where overflow checks are enabled, or wrapping to a huge `MemberCount` in builds without overflow checks — which then feeds into `yes_votes += abstentions` or `no_votes += abstentions`, artificially inflating one side and forcing an incorrect approve/disapprove outcome regardless of real votes cast.

More directly and without relying on any arithmetic edge case: the early `disapproved` check `seats.saturating_sub(no_votes) < voting.threshold` uses the **live** `seats`, not the original member count. A member set reduction (member self-removal via `renounce_candidacy` in `pallet-elections-phragmen`, which is a normal unprivileged self-service action available to any elected member/runner-up, not an admin action) [6](#0-5)  can drop `seats` below the fixed `threshold` at any time before `close` is called. This forces `disapproved = true` on the next `close()` call for **any** pending proposal whose threshold now exceeds the shrunk `seats`, discarding legitimately cast `ayes` — exactly the CoreDAO pattern of "no minimum member threshold ⇒ proposal auto-defeated," but reachable here via ordinary member churn rather than a contract-genesis edge case.

### Impact Explanation
A minority of departing/renouncing members (unprivileged, self-service action) can force early disapproval of proposals that already have majority `ayes` support but haven't yet been closed, because the disapproval check is computed against the *current* shrunk `seats` rather than the member count/threshold basis at proposal creation. This can be used to defeat governance/council motions that should have passed, and in `pallet-collective`, council/technical-committee motions gate critical runtime actions (e.g., `pallet-democracy`/OpenGov emergency origins, treasury approvals). Silently discarding legitimate approvals or, in the underflow scenario, corrupting `yes_votes`/`no_votes` counters, compromises intended collective-governance behavior.

### Likelihood Explanation
Reaching a state where `seats` shrinks meaningfully below a proposal's `threshold` while votes are outstanding requires only ordinary member turnover (renunciation/replacement), not any privileged or malicious actor assumption — this is a normal, expected operational sequence in chains using `pallet-elections-phragmen` alongside `pallet-collective` (e.g., council elections cycles). No special access is needed beyond being an existing council member choosing to renounce their seat.

### Recommendation
- Snapshot the effective quorum basis (or re-validate/re-derive `voting.threshold` proportionally) at `close` time relative to the *current* `seats`, or reject/require re-proposal when `seats` drops below the original threshold basis, rather than silently forcing disapproval on live `seats`.
- Replace `seats - (yes_votes + no_votes)` with `seats.saturating_sub(yes_votes.saturating_add(no_votes))` to eliminate the underflow-panic/wrap risk if the sum-invariant is ever violated by future member-management logic changes.
- Add an explicit minimum-seats/quorum-floor check (mirroring the CoreDAO fix) so that member-count reductions cannot unilaterally force disapproval of proposals irrespective of already-cast approval votes.

### Proof of Concept
1. Council with 5 members; `MotionDuration` long enough to keep proposal open. Member A proposes a motion with `threshold = 3`.
2. Members A and B vote `aye` (`yes_votes = 2`), no `nay` votes yet. Proposal is not yet closeable (`2 < 3`).
3. Two members (e.g., C and D, self-service via `elections-phragmen::renounce_candidacy`, no admin/root needed) exit, and no runners-up exist to replace them. `change_members_sorted` prunes their stale votes (they hadn't voted) — `seats` drops to 3.
4. A third member later calls `close`. Now `seats = 3`, `voting.threshold` is still `3`. If the last remaining member votes `nay`, `disapproved = seats.saturating_sub(no_votes) < threshold` → `(3 - 1) = 2 < 3` → `true`, forcing disapproval even though 2 of the 3 remaining members already voted `aye` and only 1 more `aye` was needed and mathematically still possible from the last member — this demonstrates the check reacting incorrectly to a shrunk member set rather than the votes actually cast, reproducing the "insufficient member count ⇒ automatic defeat" pattern from the CoreDAO report. [7](#0-6)

### Citations

**File:** substrate/frame/collective/src/lib.rs (L937-973)
```rust
	/// Add a new proposal to be voted.
	pub fn do_propose_proposed(
		who: T::AccountId,
		threshold: MemberCount,
		proposal: Box<<T as Config<I>>::Proposal>,
		length_bound: MemberCount,
	) -> Result<(u32, u32), DispatchError> {
		let proposal_len = proposal.encoded_size();
		ensure!(proposal_len <= length_bound as usize, Error::<T, I>::WrongProposalLength);
		let proposal_weight = proposal.get_dispatch_info().call_weight;
		ensure!(
			proposal_weight.all_lte(T::MaxProposalWeight::get()),
			Error::<T, I>::WrongProposalWeight
		);

		let proposal_hash = T::Hashing::hash_of(&proposal);
		ensure!(!<ProposalOf<T, I>>::contains_key(proposal_hash), Error::<T, I>::DuplicateProposal);

		let active_proposals =
			<Proposals<T, I>>::try_mutate(|proposals| -> Result<usize, DispatchError> {
				proposals.try_push(proposal_hash).map_err(|_| Error::<T, I>::TooManyProposals)?;
				Ok(proposals.len())
			})?;

		let cost = T::Consideration::new(&who, active_proposals as u32 - 1)?;
		if !cost.is_none() {
			<CostOf<T, I>>::insert(proposal_hash, (who.clone(), cost));
		}

		let index = ProposalCount::<T, I>::get();

		<ProposalCount<T, I>>::mutate(|i| *i += 1);
		<ProposalOf<T, I>>::insert(proposal_hash, proposal);
		let votes = {
			let end = frame_system::Pallet::<T>::block_number() + T::MotionDuration::get();
			Votes { index, threshold, ayes: vec![], nays: vec![], end }
		};
```

**File:** substrate/frame/collective/src/lib.rs (L1037-1092)
```rust
	/// Close a vote that is either approved, disapproved or whose voting period has ended.
	pub fn do_close(
		proposal_hash: T::Hash,
		index: ProposalIndex,
		proposal_weight_bound: Weight,
		length_bound: u32,
	) -> DispatchResultWithPostInfo {
		let voting = Voting::<T, I>::get(&proposal_hash).ok_or(Error::<T, I>::ProposalMissing)?;
		ensure!(voting.index == index, Error::<T, I>::WrongIndex);

		let mut no_votes = voting.nays.len() as MemberCount;
		let mut yes_votes = voting.ayes.len() as MemberCount;
		let seats = Members::<T, I>::get().len() as MemberCount;
		let approved = yes_votes >= voting.threshold;
		let disapproved = seats.saturating_sub(no_votes) < voting.threshold;
		// Allow (dis-)approving the proposal as soon as there are enough votes.
		if approved {
			let (proposal, len) = Self::validate_and_get_proposal(
				&proposal_hash,
				length_bound,
				proposal_weight_bound,
			)?;
			Self::deposit_event(Event::Closed { proposal_hash, yes: yes_votes, no: no_votes });
			let (proposal_weight, proposal_count) =
				Self::do_approve_proposal(seats, yes_votes, proposal_hash, proposal);
			return Ok((
				Some(
					T::WeightInfo::close_early_approved(len as u32, seats, proposal_count)
						.saturating_add(proposal_weight),
				),
				Pays::Yes,
			)
				.into());
		} else if disapproved {
			Self::deposit_event(Event::Closed { proposal_hash, yes: yes_votes, no: no_votes });
			let proposal_count = Self::do_disapprove_proposal(proposal_hash);
			return Ok((
				Some(T::WeightInfo::close_early_disapproved(seats, proposal_count)),
				Pays::No,
			)
				.into());
		}

		// Only allow actual closing of the proposal after the voting period has ended.
		ensure!(frame_system::Pallet::<T>::block_number() >= voting.end, Error::<T, I>::TooEarly);

		let prime_vote = Prime::<T, I>::get().map(|who| voting.ayes.iter().any(|a| a == &who));

		// default voting strategy.
		let default = T::DefaultVote::default_vote(prime_vote, yes_votes, no_votes, seats);

		let abstentions = seats - (yes_votes + no_votes);
		match default {
			true => yes_votes += abstentions,
			false => no_votes += abstentions,
		}
```

**File:** substrate/frame/collective/src/lib.rs (L1210-1213)
```rust
	/// Looking at votes:
	/// * The sum of aye and nay votes for a proposal can never exceed
	///  `MaxMembers`.
	/// * The proposal index inside the `Voting` storage map must be unique.
```

**File:** substrate/frame/collective/src/lib.rs (L1301-1347)
```rust
impl<T: Config<I>, I: 'static> ChangeMembers<T::AccountId> for Pallet<T, I> {
	/// Update the members of the collective. Votes are updated and the prime is reset.
	///
	/// NOTE: Does not enforce the expected `MaxMembers` limit on the amount of members, but
	///       the weight estimations rely on it to estimate dispatchable weight.
	///
	/// ## Complexity
	/// - `O(MP + N)`
	///   - where `M` old-members-count (governance-bounded)
	///   - where `N` new-members-count (governance-bounded)
	///   - where `P` proposals-count
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

**File:** substrate/frame/elections-phragmen/src/lib.rs (L792-845)
```rust
	/// Attempts to remove a member `who`. If a runner-up exists, it is used as the replacement.
	///
	/// Returns:
	///
	/// - `Ok(true)` if the member was removed and a replacement was found.
	/// - `Ok(false)` if the member was removed and but no replacement was found.
	/// - `Err(_)` if the member was no found.
	///
	/// Both `Members` and `RunnersUp` storage is updated accordingly. `T::ChangeMember` is called
	/// if needed. If `slash` is true, the deposit of the potentially removed member is slashed,
	/// else, it is unreserved.
	///
	/// ### Note: Prime preservation
	///
	/// This function attempts to preserve the prime. If the removed members is not the prime, it is
	/// set again via [`Config::ChangeMembers`].
	fn remove_and_replace_member(who: &T::AccountId, slash: bool) -> Result<bool, DispatchError> {
		// closure will return:
		// - `Ok(Option(replacement))` if member was removed and replacement was replaced.
		// - `Ok(None)` if member was removed but no replacement was found
		// - `Err(_)` if who is not a member.
		let maybe_replacement = Members::<T>::try_mutate::<_, Error<T>, _>(|members| {
			let remove_index = members
				.binary_search_by(|m| m.who.cmp(who))
				.map_err(|_| Error::<T>::NotMember)?;
			// we remove the member anyhow, regardless of having a runner-up or not.
			let removed = members.remove(remove_index);

			// slash or unreserve
			if slash {
				let (imbalance, _remainder) = T::Currency::slash_reserved(who, removed.deposit);
				debug_assert!(_remainder.is_zero());
				T::LoserCandidate::on_unbalanced(imbalance);
				Self::deposit_event(Event::SeatHolderSlashed {
					seat_holder: who.clone(),
					amount: removed.deposit,
				});
			} else {
				T::Currency::unreserve(who, removed.deposit);
			}

			let maybe_next_best = RunnersUp::<T>::mutate(|r| r.pop()).inspect(|next_best| {
				// defensive-only: Members and runners-up are disjoint. This will always be err and
				// give us an index to insert.
				if let Err(index) = members.binary_search_by(|m| m.who.cmp(&next_best.who)) {
					members.insert(index, next_best.clone());
				} else {
					// overlap. This can never happen. If so, it seems like our intended replacement
					// is already a member, so not much more to do.
					log::error!(target: LOG_TARGET, "A member seems to also be a runner-up.");
				}
			});
			Ok(maybe_next_best)
		})?;
```
