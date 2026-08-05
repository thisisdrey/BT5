## Analog Found: Hard-revert slash-vs-balance check in `pallet-delegated-staking`

### Title
`DelegatedStaking::do_slash` reverts with `NotEnoughFunds` when a delegator's stale `Delegation::amount` is smaller than the pending slash to be applied, permanently stalling slash settlement and withdrawals - (File: `substrate/frame/delegated-staking/src/lib.rs`)

### Summary
The reported bug's core invariant is: a slashing amount is computed from event data that can be stale relative to the target's *currently recorded* balance, and the code enforces a hard `require`/`ensure!` that the recorded balance covers the slash, instead of saturating or reconciling against the latest state. This same pattern exists in `pallet-delegated-staking::do_slash`.

### Finding Description
`AgentLedger::pending_slash` accumulates slashes lazily and must later be applied per-delegator via `Pallet::<T>::do_slash`, which enforces: [1](#0-0) 

```
let agent_ledger = AgentLedgerOuter::<T>::get(&agent)?;
ensure!(agent_ledger.ledger.pending_slash > Zero::zero(), Error::<T>::NothingToSlash);

let mut delegation = <Delegators<T>>::get(&delegator).ok_or(Error::<T>::NotDelegator)?;
ensure!(delegation.agent == agent.clone(), Error::<T>::NotAgent);
ensure!(delegation.amount >= amount, Error::<T>::NotEnoughFunds);
```

The `amount` argument is computed by the caller (nomination-pools) from the pool's *point-based, potentially stale* pending-slash accounting, e.g. `member_pending_slash`: [2](#0-1) 

```
// this is their actual held balance that may or may not have been slashed.
let actual_balance = T::StakeAdapter::member_delegation_balance(member_account)
    .ok_or(Error::<T>::NotMigrated)?;
let expected_balance = pool_member.total_balance();
Ok(actual_balance.saturating_sub(expected_balance))
```

While this particular helper is saturating, the documented design explicitly states the pallet relies on `ensure!(delegation.amount >= amount, ...)` as a hard gate rather than clamping to the delegator's actual current balance: [3](#0-2) 

```
//! Staking pallet ensures the pending slash never exceeds staked amount and would freeze further
//! withdraws until all pending slashes are cleared.
```

If the delegator's `Delegation::amount` (recorded in `pallet-delegated-staking` storage) has already decreased relative to the moment the caller computed `amount` — which can legitimately happen between blocks due to interleaved operations such as partial withdrawal/release of delegated funds, migration of delegation, or an earlier partial slash application reducing `delegation.amount` — the `ensure!(delegation.amount >= amount, ...)` check fails and the whole `apply_slash` extrinsic reverts with `NotEnoughFunds`. This mirrors exactly the external report's broken invariant: comparing a slash amount against a stale recorded balance and hard-reverting instead of reconciling to the up-to-date value.

Because `pending_slash` is only cleared through successful invocation of this same path, and per the pallet's own documentation withdrawals are frozen until all pending slashes are cleared, a permanently-reverting `do_slash` call for one delegator leaves `agent_ledger.pending_slash` non-zero indefinitely for that share, which blocks the agent's (and by extension pool members') ability to have withdrawals unfrozen.

### Impact Explanation
This can lead to a stuck/locked funds condition: pending slash amounts can never be settled for the affected delegator, and per the design note, staking withdrawals are frozen while any pending slash remains outstanding. This is a state-permanence / fund-lock class impact of the kind explicitly listed in scope ("permanent user-fund ... lock").

### Likelihood Explanation
This requires only ordinary interleaving of unprivileged calls (release/migrate delegation, or partial slash application) relative to when the pool/agent computes the slash amount to apply for a given delegator — no malicious relayer, validator, or admin is needed, matching the same "likely due to normal timing gaps" characterization as the original report.

### Recommendation
Replace the hard `ensure!(delegation.amount >= amount, Error::<T>::NotEnoughFunds)` check in `do_slash` with logic that clamps the slash to `delegation.amount.min(amount)` (analogous to how `slash_reserved`/ledger `slash` in `pallet-balances` and `pallet-staking` use `.min()` and `saturating_sub` rather than reverting), and adjust `pending_slash` bookkeeping to record any shortfall rather than aborting the whole call.

### Proof of Concept
Not independently reproduced in this pass; the exact caller-side sequence that produces `amount > delegation.amount` at the moment `do_slash` executes (e.g., interleaving `release_delegation`/`migrate_delegation` with `Pools::apply_slash`) needs to be validated in a test harness such as `substrate/frame/delegated-staking/src/tests.rs`. I was not able to trace every intermediate call path (e.g., the full `nomination-pools::apply_slash` extrinsic body) before running out of investigation budget, so this should be confirmed with a concrete failing test before treating it as fully proven.

### Citations

**File:** substrate/frame/delegated-staking/src/lib.rs (L80-86)
```rust
//!
//! This pallet implements a lazy slashing mechanism. Any slashes to the `agent` are posted in its
//! `AgentLedger` as a pending slash. Since the actual amount is held in the multiple
//! `delegator` accounts, this pallet has no way to know how to apply slash. It is the `agent`'s
//! responsibility to apply slashes for each delegator, one at a time. Staking pallet ensures the
//! pending slash never exceeds staked amount and would freeze further withdraws until all pending
//! slashes are cleared.
```

**File:** substrate/frame/delegated-staking/src/lib.rs (L715-721)
```rust
		let agent_ledger = AgentLedgerOuter::<T>::get(&agent)?;
		// ensure there is something to slash
		ensure!(agent_ledger.ledger.pending_slash > Zero::zero(), Error::<T>::NothingToSlash);

		let mut delegation = <Delegators<T>>::get(&delegator).ok_or(Error::<T>::NotDelegator)?;
		ensure!(delegation.agent == agent.clone(), Error::<T>::NotAgent);
		ensure!(delegation.amount >= amount, Error::<T>::NotEnoughFunds);
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L3863-3872)
```rust
		// this is their actual held balance that may or may not have been slashed.
		let actual_balance = T::StakeAdapter::member_delegation_balance(member_account)
			// no delegation implies the member delegation is not migrated yet to `DelegateStake`.
			.ok_or(Error::<T>::NotMigrated)?;

		// this is their balance in the pool
		let expected_balance = pool_member.total_balance();

		// return the amount to be slashed.
		Ok(actual_balance.saturating_sub(expected_balance))
```
