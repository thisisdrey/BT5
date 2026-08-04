### Title
`pallet-conviction-voting` tally weight is not re-synced to an account's balance after the balance is reduced outside of `remove_vote`, letting a poll's `Tally` stay inflated relative to real backing - ([File: substrate/frame/conviction-voting/src/lib.rs])

### Summary
This is the direct analog of the Olympus finding: `try_vote` checks `vote.balance() <= T::Currency::total_balance(who)` only at the moment a vote is cast, and then adds that fixed weight into the poll's `Tally` (`substrate/frame/conviction-voting/src/lib.rs:432-462`). The `Tally` is never re-derived from the voter's current balance; it is only decremented when the voter (or an authorized third party under `UnvoteScope::Any`) explicitly calls `remove_vote`, which invokes `try_remove_vote` (`substrate/frame/conviction-voting/src/lib.rs:481-563`) to subtract the previously-recorded `AccountVote` from the tally. If the account's total balance is reduced by any mechanism that does not go through `remove_vote` — most notably a slash, which directly burns `total_balance` and is explicitly not blocked by voting locks (`extend_lock` at line 708 sets `WithdrawReasons::except(WithdrawReasons::RESERVE)`, which restricts ordinary transfers but does nothing to prevent forced removal of funds via slashing) — the previously cast vote weight remains fully counted in `tally.ayes/nays/support` even though the account no longer holds the balance that justified it.

### Finding Description
- `try_vote` (lines 427-473) inserts `vote` into the poll's `votes` list and calls `tally.add(vote)` / `tally.increase(approve, delegations)`, permanently baking the voter's balance-derived weight into the `Tally` structure stored under `T::Polls`. [1](#0-0) 
- The only balance check performed is `vote.balance() <= T::Currency::total_balance(who)` at cast time, with no ongoing invariant that the recorded vote weight tracks the voter's live balance. [2](#0-1) 
- Removal of a stale vote from the tally happens only inside `try_remove_vote`, which requires an explicit call (`remove_vote`/`remove_other_vote`) from the voter or, once the poll is completed/expired, from anyone under the `NoPermissionYet`/`UnvoteScope` rules. [3](#0-2) 
- `extend_lock` only restricts `WithdrawReasons` other than `RESERVE`; it does not, and cannot, prevent a slash (which force-burns balance irrespective of locks) from reducing `total_balance` below the amount that was used to compute the already-recorded `tally` weight. [4](#0-3) 

The result is structurally identical to the Olympus bug: an aggregate decision value (`totalEndorsementsForProposal` there, `Tally.ayes/nays/support` here) is derived once from a balance and never re-synchronized when that balance later decreases through a path other than the aggregate's own "undo" entrypoint (`endorseProposal`'s self-reapply there, `remove_vote` here).

### Impact Explanation
`pallet-referenda` uses the poll's `Tally.support`/`ayes`/`nays` to evaluate approval and confirmation curves that decide whether a public referendum passes and its call is dispatched. If a voter's recorded vote weight is not removed after their balance is slashed, the referendum's measured "support" and "approval" no longer reflect real, currently-held stake, but the on-chain decision logic still treats it as valid backing. This can let a referendum reach its confirmation threshold using vote weight that no longer exists, causing dispatch of a call (potentially privileged, e.g. `Root`-track referenda) that would not have passed against the account's real, current balance. This falls under "runtime bugs that compromise intended behavior" / stale/forged state acceptance for governance dispatch.

### Likelihood Explanation
This is not attacker-controlled at will in the strict sense (it depends on a slash event reducing an already-voted account's balance), so it is more of a systemic accounting gap than a directly exploitable griefing primitive; however, it requires no malicious admin, governance actor, or off-chain privileged party — the trigger is a normal protocol action (a slash) acting on an account that already cast a vote, and no code path re-syncs or clamps the tally afterward. The team's own upstream acknowledgment pattern for the Olympus report ("hard to adjust based on balance because there's no events/callbacks") mirrors exactly this class of gap in `conviction-voting`, since no `OnSlash`/`OnReducedBalance` hook exists to trigger tally correction.

### Recommendation
Either (a) add a hook invoked on slash/force-reduction of a locked/voting balance that walks `VotingFor` for the affected account and clamps/removes the corresponding vote's contribution from the relevant poll's `Tally`, or (b) re-validate `vote.balance() <= T::Currency::total_balance(who)` at tally-consumption time (e.g., when `pallet-referenda` reads `Tally` for confirmation) and clamp any excess. At minimum, document and enforce that a slashed voter's stale votes must be purged before a poll can confirm, rather than relying solely on voluntary `remove_vote` calls.

### Proof of Concept
1. Account `A` bonds/holds balance `100` and casts `Voting::vote(A, poll, aye(100, conviction=1x))`, which adds `100` (times conviction) into the poll's `Tally.ayes`/`support` via `try_vote` (`substrate/frame/conviction-voting/src/lib.rs:432-462`).
2. `A` is later slashed for an unrelated offence (e.g., staking equivocation), burning `total_balance(A)` down to near `0` via the balances pallet's slash path, which is not blocked by the `CONVICTION_VOTING_ID` lock (`extend_lock`, line 708, excludes only `RESERVE` withdraw reason, and slashing bypasses locks entirely).
3. `A` never calls `remove_vote`. `VotingFor::<T,I>::get(A, class)` still shows the full `100`-weight vote, and the poll's `Tally` still counts it in full.
4. The referendum's approval/support curve in `pallet-referenda` is evaluated using this stale, inflated `Tally`, potentially confirming a proposal that would not meet the threshold if computed against `A`'s real, current (slashed) balance.

### Citations

**File:** substrate/frame/conviction-voting/src/lib.rs (L432-462)
```rust
		ensure!(
			vote.balance() <= T::Currency::total_balance(who),
			Error::<T, I>::InsufficientFunds
		);
		// Call on_vote hook
		T::VotingHooks::on_before_vote(who, poll_index, vote)?;

		T::Polls::try_access_poll(poll_index, |poll_status| {
			let (tally, class) = poll_status.ensure_ongoing().ok_or(Error::<T, I>::NotOngoing)?;
			VotingFor::<T, I>::try_mutate(who, &class, |voting| {
				if let Voting::Casting(Casting { ref mut votes, delegations, .. }) = voting {
					match votes.binary_search_by_key(&poll_index, |i| i.0) {
						Ok(i) => {
							// Shouldn't be possible to fail, but we handle it gracefully.
							tally.remove(votes[i].1).ok_or(ArithmeticError::Underflow)?;
							if let Some(approve) = votes[i].1.as_standard() {
								tally.reduce(approve, *delegations);
							}
							votes[i].1 = vote;
						},
						Err(i) => {
							votes
								.try_insert(i, (poll_index, vote))
								.map_err(|_| Error::<T, I>::MaxVotesReached)?;
						},
					}
					// Shouldn't be possible to fail, but we handle it gracefully.
					tally.add(vote).ok_or(ArithmeticError::Overflow)?;
					if let Some(approve) = vote.as_standard() {
						tally.increase(approve, *delegations);
					}
```

**File:** substrate/frame/conviction-voting/src/lib.rs (L481-512)
```rust
	fn try_remove_vote(
		who: &T::AccountId,
		poll_index: PollIndexOf<T, I>,
		class_hint: Option<ClassOf<T, I>>,
		scope: UnvoteScope,
	) -> DispatchResult {
		let class = class_hint
			.or_else(|| Some(T::Polls::as_ongoing(poll_index)?.1))
			.ok_or(Error::<T, I>::ClassNeeded)?;
		VotingFor::<T, I>::try_mutate(who, class, |voting| {
			if let Voting::Casting(Casting { ref mut votes, delegations, ref mut prior }) = voting {
				let i = votes
					.binary_search_by_key(&poll_index, |i| i.0)
					.map_err(|_| Error::<T, I>::NotVoter)?;
				let v = votes.remove(i);

				T::Polls::try_access_poll(poll_index, |poll_status| match poll_status {
					PollStatus::Ongoing(tally, _) => {
						ensure!(matches!(scope, UnvoteScope::Any), Error::<T, I>::NoPermission);
						// Shouldn't be possible to fail, but we handle it gracefully.
						tally.remove(v.1).ok_or(ArithmeticError::Underflow)?;
						if let Some(approve) = v.1.as_standard() {
							tally.reduce(approve, *delegations);
						}
						Self::deposit_event(Event::VoteRemoved {
							who: who.clone(),
							vote: v.1,
							poll_index,
						});
						T::VotingHooks::on_remove_vote(who, poll_index, Status::Ongoing);
						Ok(())
					},
```

**File:** substrate/frame/conviction-voting/src/lib.rs (L708-729)
```rust
	fn extend_lock(who: &T::AccountId, class: &ClassOf<T, I>, amount: BalanceOf<T, I>) {
		ClassLocksFor::<T, I>::mutate(who, |locks| {
			match locks.iter().position(|x| &x.0 == class) {
				Some(i) => locks[i].1 = locks[i].1.max(amount),
				None => {
					let ok = locks.try_push((class.clone(), amount)).is_ok();
					debug_assert!(
						ok,
						"Vec bounded by number of classes; \
						all items in Vec associated with a unique class; \
						qed"
					);
				},
			}
		});
		T::Currency::extend_lock(
			CONVICTION_VOTING_ID,
			who,
			amount,
			WithdrawReasons::except(WithdrawReasons::RESERVE),
		);
	}
```
