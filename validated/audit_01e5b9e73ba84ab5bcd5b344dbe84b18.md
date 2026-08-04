### Title
Unbounded instant-lock voting power in `pallet-elections-phragmen::vote` allows flash-stake manipulation of council elections with zero holding-period cost - (File: `substrate/frame/elections-phragmen/src/lib.rs`)

### Summary
The external report's core broken invariant is that a time-scaled multiplier grants near-maximum voting power for an arbitrarily short lock duration, letting an attacker briefly park capital, cast a vote, and withdraw with only a marginal early-exit penalty. The Substrate analog is `pallet_elections_phragmen::vote`, whose voting weight is the raw locked `value` with **no minimum holding period whatsoever**, and `remove_voter`, which releases that lock and deposit **instantly and penalty-free**. This is strictly worse than the reported bug: there the attacker at least paid an early-withdrawal fee/penalty; here there is none.

### Finding Description
`Pallet::vote` lets any signed account set (or update) a vote for council candidates, reserving a bond and locking `value` (capped at free balance) via `T::Currency::set_lock`: [1](#0-0) 

There is no check tying the size of `value` (i.e., the voter's weight fed into `CurrencyToVote`) to how long the funds have been locked, nor any minimum time the vote must remain in place before it counts toward the next election round. The election round is deterministically triggered purely by block number arithmetic (`BlockNumber % TermDuration == 0`), documented here: [2](#0-1) 

A voter can therefore call `vote()` in the block immediately preceding the round-triggering block with a very large `value` (comparable to the Solidity bug's "stake for 1 second and vote"), be counted at full weight in that round's Phragmén tally, and then call `remove_voter()` immediately after the round completes to reclaim the entire locked stake and bond with **no delay and no penalty**, confirmed by the pallet's own test: [3](#0-2) 

This mirrors the reported flaw exactly: `calMintStakingPower` grants near-full multiplier (0.88x) for `rd` (remaining duration) as low as 1 second because the guard only checks `rd > 0` rather than a meaningful minimum. Here, `pallet-elections-phragmen` grants full multiplier (1x, i.e., the entire locked value counts toward `CurrencyToVote`) for a holding period that can be a single block, because there is no `rd`-style guard at all — only `value > T::Currency::minimum_balance()`: [4](#0-3) 

### Impact Explanation
Council/committee elections decided by `pallet-elections-phragmen` typically control governance-adjacent origins (technical committee composition, motions, spending approvals in many production runtimes). An attacker who can transiently acquire a large balance (e.g., via an on-chain lending/DEX flash mechanism, an exchange withdrawal window, or simply large idle capital) can:
1. Call `vote()` with a large `value` just before the round boundary.
2. Get counted at full weight in the Phragmén election, swinging the composition of `Members`/`RunnersUp`.
3. Call `remove_voter()` immediately afterward to fully unreserve the deposit and remove the lock, at zero cost beyond transaction fees — unlike the reported Solidity case which at least imposed an early-exit fee/penalty.

This is a governance-composition manipulation with no time-cost barrier, satisfying the "unauthorized execution or origin escalation" / "runtime bugs that compromise intended behavior" impact class, since council membership is exactly the kind of state that is supposed to reflect sustained economic commitment, not a single-block flash stake.

### Likelihood Explanation
Any signed, unprivileged account can invoke `vote` and `remove_voter` — no admin, governance, or validator privilege is required. The only barrier is temporarily holding/controlling enough balance, which is a purely economic (not protocol) constraint and can be satisfied via flash-loan-like mechanisms on chains where the voting currency is liquid/borrowable. The exploit requires precise timing around `TermDuration` boundaries, which are fully deterministic and publicly computable on-chain (`BlockNumber % TermDuration == 0`), making the attack trivially schedulable.

### Recommendation
Introduce a minimum lock/holding duration (analogous to `rd > 1 week` in the referenced fix) before a voter's stake counts at full weight in an election round — e.g., require that the vote (or an increase in `value`) must have existed for at least one full round/`TermDuration` before it is eligible to be tallied, or apply a time-decayed weighting so that votes placed immediately before a round boundary count proportionally less. Alternatively, enforce a mandatory post-round lock on `value` before `remove_voter` can fully release funds, so that gaining voting power always carries a real, non-trivial economic holding cost.

### Proof of Concept
1. Attacker observes `TermDuration` and computes the next round-triggering block `N` (`N % TermDuration == 0`).
2. At block `N - 1`, attacker (having briefly acquired a large balance) calls `Elections::vote(candidates, large_value)`, reserving the vote deposit and locking `large_value`. [5](#0-4) 
3. At block `N`, the election round executes and tallies `large_value` at full weight toward the attacker's chosen candidates, per `CurrencyToVote`.
4. At block `N + 1`, attacker calls `Elections::remove_voter()`, instantly restoring their full balance and unreserving the deposit, exactly as demonstrated in the existing test `remove_voter_should_work`. [6](#0-5) 
5. Net cost to the attacker: transaction fees only — no lock-duration-proportional penalty exists, unlike the reported Solidity bug's early-withdrawal fee.

### Citations

**File:** substrate/frame/elections-phragmen/src/lib.rs (L7-14)
```rust
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
// 	http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
```

**File:** substrate/frame/elections-phragmen/src/lib.rs (L371-422)
```rust
		pub fn vote(
			origin: OriginFor<T>,
			votes: Vec<T::AccountId>,
			#[pallet::compact] value: BalanceOf<T>,
		) -> DispatchResultWithPostInfo {
			let who = ensure_signed(origin)?;

			ensure!(
				votes.len() <= T::MaxVotesPerVoter::get() as usize,
				Error::<T>::MaximumVotesExceeded
			);
			ensure!(!votes.is_empty(), Error::<T>::NoVotes);

			let candidates_count = Candidates::<T>::decode_len().unwrap_or(0);
			let members_count = Members::<T>::decode_len().unwrap_or(0);
			let runners_up_count = RunnersUp::<T>::decode_len().unwrap_or(0);

			// can never submit a vote of there are no members, and cannot submit more votes than
			// all potential vote targets.
			// addition is valid: candidates, members and runners-up will never overlap.
			let allowed_votes =
				candidates_count.saturating_add(members_count).saturating_add(runners_up_count);
			ensure!(!allowed_votes.is_zero(), Error::<T>::UnableToVote);
			ensure!(votes.len() <= allowed_votes, Error::<T>::TooManyVotes);

			ensure!(value > T::Currency::minimum_balance(), Error::<T>::LowBalance);

			// Reserve bond.
			let new_deposit = Self::deposit_of(votes.len());
			let Voter { deposit: old_deposit, .. } = Voting::<T>::get(&who);
			match new_deposit.cmp(&old_deposit) {
				Ordering::Greater => {
					// Must reserve a bit more.
					let to_reserve = new_deposit - old_deposit;
					T::Currency::reserve(&who, to_reserve)
						.map_err(|_| Error::<T>::UnableToPayBond)?;
				},
				Ordering::Equal => {},
				Ordering::Less => {
					// Must unreserve a bit.
					let to_unreserve = old_deposit - new_deposit;
					let _remainder = T::Currency::unreserve(&who, to_unreserve);
					debug_assert!(_remainder.is_zero());
				},
			};

			// Amount to be locked up.
			let locked_stake = value.min(T::Currency::free_balance(&who));
			T::Currency::set_lock(T::PalletId::get(), &who, locked_stake, WithdrawReasons::all());

			Voting::<T>::insert(&who, Voter { votes, deposit: new_deposit, stake: locked_stake });
			Ok(None::<Weight>.into())
```

**File:** substrate/frame/elections-phragmen/src/lib.rs (L2177-2201)
```rust
	#[test]
	fn remove_voter_should_work() {
		ExtBuilder::default().voter_bond(8).build_and_execute(|| {
			assert_ok!(submit_candidacy(RuntimeOrigin::signed(5)));

			assert_ok!(vote(RuntimeOrigin::signed(2), vec![5], 20));
			assert_ok!(vote(RuntimeOrigin::signed(3), vec![5], 30));

			assert_eq_uvec!(all_voters(), vec![2, 3]);
			assert_eq!(balances(&2), (12, 8));
			assert_eq!(locked_stake_of(&2), 12);
			assert_eq!(balances(&3), (22, 8));
			assert_eq!(locked_stake_of(&3), 22);
			assert_eq!(votes_of(&2), vec![5]);
			assert_eq!(votes_of(&3), vec![5]);

			assert_ok!(Elections::remove_voter(RuntimeOrigin::signed(2)));

			assert_eq_uvec!(all_voters(), vec![3]);
			assert!(votes_of(&2).is_empty());
			assert_eq!(locked_stake_of(&2), 0);

			assert_eq!(balances(&2), (20, 0));
			assert_eq!(pallet_balances::Locks::<Test>::get(&2).len(), 0);
		});
```
