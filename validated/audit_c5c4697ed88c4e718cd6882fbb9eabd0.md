## Title
Conviction-voting lock permits `RESERVE`-reason withdrawals, letting a voter's backing balance be slashed away while the poll tally still counts the original vote weight - ([File: substrate/frame/conviction-voting/src/lib.rs])

## Summary
The Mento report's core broken invariant is: voting power ("veMENTO") is recorded once and never re-validated against the user's *current* locked/underlying balance, so a user can drain the underlying stake (via the `stopped` bypass in `getAvailableForWithdraw`) while still keeping full voting power. The exact analog exists in `pallet-conviction-voting` (and identically in `pallet-democracy`): the vote weight (`AccountVote::balance()`) is checked against `T::Currency::total_balance` only once, at the moment of `vote`/`delegate`, and is then "protected" by a `Currency::extend_lock`/`set_lock` that explicitly excludes the `RESERVE` withdraw reason. Because `reserve()` bypasses this lock, a voter's tokens can leave `free` balance and later be irrecoverably burned via `slash_reserved` in another pallet, permanently reducing the account's real balance far below the amount that is still tallied for an ongoing poll - the vote/tally is never re-checked or adjusted.

## Finding Description
In `try_vote` the only balance check is performed once, at cast time: [1](#0-0) 

The lock that is supposed to "back" this recorded vote weight is created with: [2](#0-1) 

and re-applied identically in `update_lock`: [3](#0-2) 

Both calls use `WithdrawReasons::except(WithdrawReasons::RESERVE)`, which the library itself documents as "all reasons except one": [4](#0-3) 

That means the CONVICTION_VOTING_ID lock restricts `TRANSACTION_PAYMENT`, `TRANSFER`, `FEE`, and `TIP` reasons, but explicitly **does not** restrict withdrawals tagged `RESERVE`. Any pallet that calls `Currency::reserve()` on the voter's account (identity deposits, elections-phragmen candidacy bonds, preimage/proposal deposits, multisig deposits, etc.) can move the "locked" tokens from `free` to `reserved` balance despite the conviction-voting lock. Once reserved, those tokens can be permanently destroyed by a `slash_reserved` call in the owning pallet (e.g., an election candidate who is not elected has their candidacy bond slashed automatically at election-end in `pallet-elections-phragmen`, with no admin/governance action required — this is a normal protocol outcome, not "governance abuse").

The same pattern is duplicated verbatim in `pallet-democracy`'s `try_vote`/`update_lock`: [5](#0-4) [6](#0-5) 

After the slash occurs, `T::Currency::total_balance(who)` is now lower than `vote.balance()` recorded in `VotingFor`/`VotingOf` storage, yet:
- the ongoing poll's `Tally` still counts the full `vote.balance()` weight (set once in `tally.add(vote)` inside `try_vote`),
- `update_lock`/`unlock` only ever *reduces* the lock amount based on `locked_balance()` derived from the stored vote record — never re-derives it from the account's *actual remaining* balance,
- there is no hook anywhere that re-checks `total_balance >= vote.balance()` after the vote is cast.

This is structurally identical to the Mento issue: a balance-backing check performed only at a single point in time (`vote`/`lock`), a separate "voting power" ledger (`Tally`/`VotingFor` vs. veMENTO "lines") that is not recomputed when the backing balance changes, and a legitimate, unprivileged withdrawal path (`RESERVE` reason / `stopped` state) that the lock/guard fails to cover.

## Impact Explanation
An account can continue to influence the outcome of an ongoing (or even already-completed but not yet finalized) referendum/poll with a voting weight that is no longer backed by real, spendable MENTO/native-token balance. This compromises the integrity of on-chain governance tallies (`pallet-conviction-voting`, `pallet-democracy`) — a runtime bug that "compromises intended behavior" of the voting subsystem, matching the Impact Gate's "runtime bugs that compromise intended behavior" and "unauthorized ... origin escalation" categories in spirit (unauthorized retention of influence disproportionate to backing economic stake). It does not require a malicious peer/validator/relayer — only an ordinary token holder using standard, permissionless pallet calls (`vote`, `reserve`-triggering extrinsics in other pallets, and passive participation in an election that slashes losing candidates' deposits).

## Likelihood Explanation
The path requires only unprivileged, standard actions:
1. Vote/delegate in `pallet-conviction-voting` or `pallet-democracy` (locks funds, but only for `except(RESERVE)` reasons).
2. Use the same account to trigger a `reserve()` in any other pallet configured in the runtime that reserves against the free balance (e.g., submit an elections-phragmen candidacy) — no privileged actor needed, and the lock does not block this.
3. Let the reserved deposit be slashed through the pallet's normal, automatic mechanics (e.g., losing an election) — again no admin/governance action, just ordinary protocol flow.

No malicious validator, relayer, governance actor, or leaked key is required, and the check-once/lock-forever pattern makes the underlying assumption ("locked ⇒ balance stays ≥ vote weight") false as soon as any RESERVE-reason consumer exists in the runtime, which is common (elections, identity, preimages, multisig, proxy deposits are all standard FRAME pallets typically composed together).

## Recommendation
- Either include `RESERVE` in the withdraw reasons restricted by the conviction-voting/democracy lock (i.e., use `WithdrawReasons::all()` rather than `except(RESERVE)`), or
- Re-validate `vote.balance() <= T::Currency::total_balance(who)` (or better, `reducible_balance`) whenever a poll's tally is read/finalized, and clamp/remove stale votes whose backing balance has been reduced below the recorded amount, analogous to the report's recommendation to "remove the corresponding lines for the delegator" when the entire locked amount is withdrawn.
- Audit all runtime configurations that combine `pallet-conviction-voting`/`pallet-democracy` with any pallet that reserves and can subsequently slash reserved balance, to confirm the RESERVE-reason gap cannot be exploited to detach voting weight from real economic backing.

## Proof of Concept
Conceptual reproduction using existing pallet APIs (mirrors the Mento PoC's stop/withdraw/vote sequence):
1. Account `A` calls `pallet_conviction_voting::vote(poll_index, AccountVote::Standard { vote, balance: X })` — locks `X` via `extend_lock(CONVICTION_VOTING_ID, A, X, WithdrawReasons::except(RESERVE))`. Poll tally now counts `X`.
2. In the same runtime, `A` calls `pallet_elections_phragmen::submit_candidacy(...)`, which internally calls `Currency::reserve(&A, CandidacyBond)` — succeeds because `RESERVE` is excluded from the conviction-voting lock, even though `A`'s free balance minus the lock would otherwise be insufficient/tight.
3. Election runs; `A` is not elected, and the pallet automatically slashes the reserved candidacy bond (`slash_reserved`), permanently destroying those tokens from `A`'s `total_balance`.
4. `A`'s `total_balance` is now less than `X` (the tallied vote weight), yet the poll's `Tally` for `poll_index` still reflects the original `X`, and no call in `try_vote`/`update_lock`/`unlock` re-checks or corrects this — `A` (or whoever `A` delegated to) retains full voting influence backed by tokens that no longer exist, exactly as in the Mento `withdraw()`-after-`stop()` scenario where veMENTO/voting power survived the loss of the underlying locked MENTO.

### Citations

**File:** substrate/frame/conviction-voting/src/lib.rs (L427-437)
```rust
	fn try_vote(
		who: &T::AccountId,
		poll_index: PollIndexOf<T, I>,
		vote: AccountVote<BalanceOf<T, I>>,
	) -> DispatchResult {
		ensure!(
			vote.balance() <= T::Currency::total_balance(who),
			Error::<T, I>::InsufficientFunds
		);
		// Call on_vote hook
		T::VotingHooks::on_before_vote(who, poll_index, vote)?;
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

**File:** substrate/frame/conviction-voting/src/lib.rs (L731-761)
```rust
	/// Rejig the lock on an account. It will never get more stringent (since that would indicate
	/// a security hole) but may be reduced from what they are currently.
	fn update_lock(class: &ClassOf<T, I>, who: &T::AccountId) {
		let class_lock_needed = VotingFor::<T, I>::mutate(who, class, |voting| {
			voting.rejig(T::BlockNumberProvider::current_block_number());
			voting.locked_balance()
		});
		let lock_needed = ClassLocksFor::<T, I>::mutate(who, |locks| {
			locks.retain(|x| &x.0 != class);
			if !class_lock_needed.is_zero() {
				let ok = locks.try_push((class.clone(), class_lock_needed)).is_ok();
				debug_assert!(
					ok,
					"Vec bounded by number of classes; \
					all items in Vec associated with a unique class; \
					qed"
				);
			}
			locks.iter().map(|x| x.1).max().unwrap_or(Zero::zero())
		});
		if lock_needed.is_zero() {
			T::Currency::remove_lock(CONVICTION_VOTING_ID, who);
		} else {
			T::Currency::set_lock(
				CONVICTION_VOTING_ID,
				who,
				lock_needed,
				WithdrawReasons::except(WithdrawReasons::RESERVE),
			);
		}
	}
```

**File:** substrate/frame/support/src/traits/tokens/misc.rs (L199-232)
```rust
bitflags::bitflags! {
	/// Reasons for moving funds out of an account.
	#[derive(Encode, Decode, MaxEncodedLen)]
	pub struct WithdrawReasons: u8 {
		/// In order to pay for (system) transaction costs.
		const TRANSACTION_PAYMENT = 0b00000001;
		/// In order to transfer ownership.
		const TRANSFER = 0b00000010;
		/// In order to reserve some funds for a later return or repatriation.
		const RESERVE = 0b00000100;
		/// In order to pay some other (higher-level) fees.
		const FEE = 0b00001000;
		/// In order to tip a validator for transaction inclusion.
		const TIP = 0b00010000;
	}
}

impl WithdrawReasons {
	/// Choose all variants except for `one`.
	///
	/// ```rust
	/// # use frame_support::traits::WithdrawReasons;
	/// # fn main() {
	/// assert_eq!(
	/// 	WithdrawReasons::FEE | WithdrawReasons::TRANSFER | WithdrawReasons::RESERVE | WithdrawReasons::TIP,
	/// 	WithdrawReasons::except(WithdrawReasons::TRANSACTION_PAYMENT),
	/// 	);
	/// # }
	/// ```
	pub fn except(one: WithdrawReasons) -> WithdrawReasons {
		let mut flags = Self::all();
		flags.toggle(one);
		flags
	}
```

**File:** substrate/frame/democracy/src/lib.rs (L1271-1317)
```rust
	/// Actually enact a vote, if legit.
	fn try_vote(
		who: &T::AccountId,
		ref_index: ReferendumIndex,
		vote: AccountVote<BalanceOf<T>>,
	) -> DispatchResult {
		let mut status = Self::referendum_status(ref_index)?;
		ensure!(vote.balance() <= T::Currency::free_balance(who), Error::<T>::InsufficientFunds);
		VotingOf::<T>::try_mutate(who, |voting| -> DispatchResult {
			if let Voting::Direct { ref mut votes, delegations, .. } = voting {
				match votes.binary_search_by_key(&ref_index, |i| i.0) {
					Ok(i) => {
						// Shouldn't be possible to fail, but we handle it gracefully.
						status.tally.remove(votes[i].1).ok_or(ArithmeticError::Underflow)?;
						if let Some(approve) = votes[i].1.as_standard() {
							status.tally.reduce(approve, *delegations);
						}
						votes[i].1 = vote;
					},
					Err(i) => {
						votes
							.try_insert(i, (ref_index, vote))
							.map_err(|_| Error::<T>::MaxVotesReached)?;
					},
				}
				Self::deposit_event(Event::<T>::Voted { voter: who.clone(), ref_index, vote });
				// Shouldn't be possible to fail, but we handle it gracefully.
				status.tally.add(vote).ok_or(ArithmeticError::Overflow)?;
				if let Some(approve) = vote.as_standard() {
					status.tally.increase(approve, *delegations);
				}
				Ok(())
			} else {
				Err(Error::<T>::AlreadyDelegating.into())
			}
		})?;
		// Extend the lock to `balance` (rather than setting it) since we don't know what other
		// votes are in place.
		T::Currency::extend_lock(
			DEMOCRACY_ID,
			who,
			vote.balance(),
			WithdrawReasons::except(WithdrawReasons::RESERVE),
		);
		ReferendumInfoOf::<T>::insert(ref_index, ReferendumInfo::Ongoing(status));
		Ok(())
	}
```

**File:** substrate/frame/democracy/src/lib.rs (L1501-1517)
```rust
	/// a security hole) but may be reduced from what they are currently.
	fn update_lock(who: &T::AccountId) {
		let lock_needed = VotingOf::<T>::mutate(who, |voting| {
			voting.rejig(frame_system::Pallet::<T>::block_number());
			voting.locked_balance()
		});
		if lock_needed.is_zero() {
			T::Currency::remove_lock(DEMOCRACY_ID, who);
		} else {
			T::Currency::set_lock(
				DEMOCRACY_ID,
				who,
				lock_needed,
				WithdrawReasons::except(WithdrawReasons::RESERVE),
			);
		}
	}
```
