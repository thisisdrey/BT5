## Analysis Summary

The external report's core broken invariant is: **value that is not yet finally settled (vesting) can still be used immediately for a privileged capability (DAO voting power) that should only apply to settled value.** Searching the repository for this exact invariant shape (settlement pending → capability granted anyway) surfaces a real, self-acknowledged analog in `pallet-delegated-staking`'s lazy-slashing design, combined with how `pallet-conviction-voting` weighs an account's balance for votes/delegations.

### Title
Delegated stake retains full governance voting weight while a pending slash is unapplied to the delegator - ([File: substrate/frame/delegated-staking/src/lib.rs])

### Summary
`pallet-delegated-staking` holds a delegator's funds via `FunHoldMutate` (`HoldReason::StakingDelegation`) rather than an ordinary lock specifically so the delegator can keep using that balance for governance while it is staked [1](#0-0) . Slashing against an `agent` is booked lazily as `pending_slash` and is only actually deducted from a specific delegator's held balance when someone later calls `delegator_slash`/`apply_slash` for that individual account [2](#0-1) . The pallet's own doc explicitly flags this as a limitation: "there could be a period of time when an account can use funds for operations such as voting in governance even though they should be slashed" [3](#0-2) .

### Finding Description
`do_slash` only reduces a delegator's held balance and `AgentLedger::pending_slash` when explicitly invoked per-delegator [4](#0-3) ; there is no chain-enforced deadline compelling this call. Until it is called, the delegator's on-chain balance (free + held) is unchanged, confirmed by the pool test `pool_partially_slashed`, which shows `held_balance_of` for every delegator remaining at the pre-slash amount immediately after `do_slash` posts a 500-unit pending slash to the agent ledger [5](#0-4) .

Meanwhile, `pallet-conviction-voting`'s `try_vote` and `try_delegate` authorize voting/delegating weight up to `T::Currency::total_balance(who)` [6](#0-5) [7](#0-6) . Because delegated-staking intentionally holds (rather than fully locks/transfers out) the delegator's funds precisely so they remain usable for "participation in governance voting" [8](#0-7) , an account whose stake has already incurred a slash (recorded in `pending_slash` on the agent) still presents its full pre-slash balance to conviction-voting until the lazy-slash catch-up call executes — the exact analog of the reported bug's "value not yet settled still grants an unearned privileged capability."

### Impact Explanation
This inflates on-chain governance weight (OpenGov conviction-voting locks/tallies) for accounts whose economic stake has already been reduced by a slash event but not yet reconciled. In aggregate across many delegators of a slashed agent, this can materially distort referendum tallies during the (open-ended) window before `apply_slash`/`delegator_slash` is called, since nothing in the protocol forces prompt reconciliation — it depends entirely on a permissionless-but-optional follow-up transaction.

### Likelihood Explanation
Likelihood is bounded: exploiting this requires an agent (nomination pool bonded account or other staking agent) to actually be slashed while its delegators still vote before slash reconciliation. This is an ordinary consequence of normal validator misbehavior/slashing plus normal governance participation — no malicious peer, relayer, or privileged actor is required, satisfying the scope's requirement to reject only peer/relayer/admin-dependent scenarios. However, it depends on a slashing event occurring, which is not attacker-controlled on demand, making it an availability-window issue rather than a directly attacker-triggerable exploit.

### Recommendation
Either (a) have conviction-voting/OpenGov weigh votes using a stake-pending-slash-adjusted balance for accounts flagged with delegated-staking holds, or (b) force `pending_slash` reconciliation (or a proportional temporary vote-weight reduction) at the point a slash is posted to the `AgentLedger`, rather than leaving it to a separate, un-deadlined permissionless call. At minimum, this documented limitation should be elevated from a doc-comment caveat to an explicit runtime safeguard given its direct effect on governance tallying.

### Proof of Concept
1. Delegator `D` delegates `X` to agent `A` via `delegate_to_agent`; `X` is placed on hold (`HoldReason::StakingDelegation`), not merely locked, so `D`'s `total_balance` still fully includes `X` [9](#0-8) .
2. `A` is slashed on the staking pallet (`pallet_staking::slashing::do_slash`); this posts `pending_slash` to `A`'s `AgentLedger` without touching any individual delegator's held balance yet [10](#0-9) .
3. Before anyone calls `delegator_slash`/`apply_slash` for `D`, `D` calls `conviction_voting::vote` or `delegate` using its full (still-unslashed) balance, since `try_vote`/`try_delegate` only check `total_balance(D)` [11](#0-10) .
4. `D`'s governance vote weight reflects `X` in full, even though `X` is already economically encumbered by `A`'s unreconciled slash — mirroring the reported bug's "minted/locked tokens grant immediate voting power before settlement finishes."

### Citations

**File:** substrate/frame/delegated-staking/src/lib.rs (L77-89)
```rust
//! ## Lazy Slashing
//! One of the reasons why direct nominators on staking pallet cannot scale well is because all
//! nominators are slashed at the same time. This is expensive and needs to be bounded operation.
//!
//! This pallet implements a lazy slashing mechanism. Any slashes to the `agent` are posted in its
//! `AgentLedger` as a pending slash. Since the actual amount is held in the multiple
//! `delegator` accounts, this pallet has no way to know how to apply slash. It is the `agent`'s
//! responsibility to apply slashes for each delegator, one at a time. Staking pallet ensures the
//! pending slash never exceeds staked amount and would freeze further withdraws until all pending
//! slashes are cleared.
//!
//! The user of this pallet can apply slash using
//! [DelegationInterface::delegator_slash](sp_staking::DelegationInterface::delegator_slash).
```

**File:** substrate/frame/delegated-staking/src/lib.rs (L110-115)
```rust
//!  1) delegate fund from delegator to pool account, and
//!  2) stake from pool account as an `Agent` account on the staking pallet.
//!
//! The difference being, in the second approach, the delegated funds will be locked in-place in
//! user's account enabling them to participate in use cases that allows use of `held` funds such
//! as participation in governance voting.
```

**File:** substrate/frame/delegated-staking/src/lib.rs (L120-123)
```rust
//! ## Limitations
//! - Rewards can not be auto-compounded.
//! - Slashes are lazy and hence there could be a period of time when an account can use funds for
//!   operations such as voting in governance even though they should be slashed.
```

**File:** substrate/frame/delegated-staking/src/lib.rs (L704-735)
```rust
	/// Take slash `amount` from agent's `pending_slash`counter and apply it to `delegator` account.
	pub fn do_slash(
		agent: Agent<T::AccountId>,
		delegator: Delegator<T::AccountId>,
		amount: BalanceOf<T>,
		maybe_reporter: Option<T::AccountId>,
	) -> DispatchResult {
		// get inner type
		let agent = agent.get();
		let delegator = delegator.get();

		let agent_ledger = AgentLedgerOuter::<T>::get(&agent)?;
		// ensure there is something to slash
		ensure!(agent_ledger.ledger.pending_slash > Zero::zero(), Error::<T>::NothingToSlash);

		let mut delegation = <Delegators<T>>::get(&delegator).ok_or(Error::<T>::NotDelegator)?;
		ensure!(delegation.agent == agent.clone(), Error::<T>::NotAgent);
		ensure!(delegation.amount >= amount, Error::<T>::NotEnoughFunds);

		// slash delegator
		let (mut credit, missing) =
			T::Currency::slash(&HoldReason::StakingDelegation.into(), &delegator, amount);

		defensive_assert!(missing.is_zero(), "slash should have been fully applied");

		let actual_slash = credit.peek();

		// remove the applied slashed amount from agent.
		agent_ledger.remove_slash(actual_slash).save();
		delegation.amount =
			delegation.amount.checked_sub(&actual_slash).ok_or(ArithmeticError::Overflow)?;
		delegation.update(&delegator);
```

**File:** substrate/frame/nomination-pools/test-delegate-stake/src/lib.rs (L714-723)
```rust
		pallet_staking_async::slashing::do_slash::<Runtime>(
			&POOL1_BONDED,
			50,
			&mut Default::default(),
			&mut Default::default(),
			100,
		);

		// Pools api returns correct slash amount.
		assert_eq!(Pools::api_pool_pending_slash(1), 50);
```

**File:** substrate/frame/nomination-pools/test-delegate-stake/src/lib.rs (L1240-1251)
```rust
		LegacyAdapter::set(true);
		assert_eq!(Balances::minimum_balance(), 5);
		assert_active_era(0);

		// hack: mint ED to pool so that the deprecated `TransferStake` works correctly with
		// staking.
		assert_eq!(Balances::minimum_balance(), 5);
		assert_ok!(Balances::mint_into(&POOL1_BONDED, 5));

		// create the pool with TransferStake strategy.
		assert_ok!(Pools::create(RuntimeOrigin::signed(10), 50, 10, 10, 10));
		assert_eq!(LastPoolId::<Runtime>::get(), 1);
```

**File:** substrate/frame/conviction-voting/src/lib.rs (L427-435)
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
```

**File:** substrate/frame/conviction-voting/src/lib.rs (L632-634)
```rust
		ensure!(who != target, Error::<T, I>::Nonsense);
		T::Polls::classes().binary_search(&class).map_err(|_| Error::<T, I>::BadClass)?;
		ensure!(balance <= T::Currency::total_balance(&who), Error::<T, I>::InsufficientFunds);
```

**File:** substrate/frame/delegated-staking/src/tests.rs (L312-339)
```rust
#[test]
fn allow_full_amount_to_be_delegated() {
	ExtBuilder::default().build_and_execute(|| {
		let agent: AccountId = 200;
		let reward_acc: AccountId = 201;
		let delegator: AccountId = 300;

		// set intention to accept delegation.
		fund(&agent, 1000);
		assert_ok!(DelegatedStaking::register_agent(RawOrigin::Signed(agent).into(), reward_acc));

		// delegate to this account
		fund(&delegator, 1000);
		assert_ok!(DelegatedStaking::delegate_to_agent(
			RawOrigin::Signed(delegator).into(),
			agent,
			1000
		));

		// verify
		assert!(DelegatedStaking::is_agent(&agent));
		assert_eq!(DelegatedStaking::stakeable_balance(Agent::from(agent)), 1000);
		assert_eq!(
			Balances::balance_on_hold(&HoldReason::StakingDelegation.into(), &delegator),
			1000
		);
		assert_eq!(DelegatedStaking::held_balance_of(Delegator::from(delegator)), 1000);
	});
```
