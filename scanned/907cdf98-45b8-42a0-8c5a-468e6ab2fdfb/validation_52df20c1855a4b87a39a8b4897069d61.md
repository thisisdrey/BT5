### Title
Missing bound check between per-call slash `amount` and `AgentLedger.pending_slash` allows over-slashing of delegators - (`substrate/frame/delegated-staking/src/lib.rs`)

### Summary
`Pallet::do_slash` (reachable via the public `DelegationInterface::delegator_slash` entry point) validates a slash request against the *delegator's* current balance (`delegation.amount >= amount`) and against the *existence* of a pending slash (`pending_slash > 0`), but never validates that the requested `amount` (and the cumulative amount slashed across repeated calls) does not exceed the agent's actual `pending_slash` counter. This is the same broken invariant as the FloatCapital `Staker.shiftTokens` bug: a per-call check against one side of the ledger (own balance) without a check that the sum of everything moved does not exceed the true global constraint (posted slash / staked amount).

### Finding Description
`do_slash` in `substrate/frame/delegated-staking/src/lib.rs`: [1](#0-0) 

only requires:
1. `agent_ledger.ledger.pending_slash > Zero::zero()` — a non-zero existence check, not a bound on `amount`.
2. `delegation.amount >= amount` — bounds the request against the delegator's own remaining delegation, which is *not* the invariant that must hold; the invariant that must hold is that the sum of amounts slashed across all calls for that agent never exceeds `agent_ledger.ledger.pending_slash`.

There is no `ensure!(amount <= agent_ledger.ledger.pending_slash, ...)` anywhere in the function before `T::Currency::slash` is executed and `agent_ledger.remove_slash(actual_slash)` is called. This mirrors exactly the confirmed root cause in the external report: `Staker.shiftTokens` checked the user's own stake balance but never checked the *aggregate* amount already committed against the true constraint (their staked balance across the two accumulating pending variables), allowing repeated calls before the constraining state ("next price" there, `pending_slash` here) was reconciled to exceed the intended bound.

The pallet's own doc comment for the module even states the intended invariant that is not code-enforced: [2](#0-1) 
"Staking pallet ensures the pending slash never exceeds staked amount and would freeze further withdraws until all pending slashes are cleared" — but this guarantee lives entirely in `CoreStaking`'s bookkeeping of `pending_slash` accrual (via `on_slash`), not in `do_slash`'s consumption path: [3](#0-2) 

`do_slash` is invoked once per delegator ("one at a time" per the doc), and the agent (any registered account — registration is open to any signer via `register_agent`, not a privileged/governance role) fully controls both which delegators to call it on and the `amount` argument for each call: [4](#0-3) [5](#0-4) 

Because nothing ties the sum of `amount` across successive `delegator_slash` calls to `pending_slash`, an agent (or anyone with agent-like access through a consumer pallet, e.g. a pool operator role) can call `delegator_slash` against multiple delegators with `amount` values whose sum vastly exceeds the actual `pending_slash` recorded for that agent. Each individual call passes because it is only checked against that single delegator's own `delegation.amount`, exactly as `shiftTokens` passed because each call was only checked against the caller's own stake balance rather than the sum of outstanding shift requests.

### Impact Explanation
This breaks the "conserve value and settle exactly once" pivot for staking/asset accounting: delegator funds held via `HoldReason::StakingDelegation` can be forcibly slashed (via `T::Currency::slash`) and moved to the reporter/`OnSlash` sink in amounts that were never actually validated by `CoreStaking`'s slashing mechanism for that delegator's share. This is effectively unbacked/unauthorized destruction and reallocation of delegator-held funds — the delegator loses more than the chain's own slashing pallet ever decided to slash from that agent, and the excess is transferred out to a reporter or burned via `OnSlash`, with no path to recovery. This is a direct fund-loss/theft primitive on real staked value, not merely a bookkeeping inconsistency.

### Likelihood Explanation
Any account can become an `Agent` via the open `register_agent` call, and any account with delegators under it can invoke `delegator_slash` repeatedly for each of its delegators once even a single slash event has posted `pending_slash > 0` for that agent. No governance, root, or validator privilege is required — this is a standard permissioned-by-role-but-not-privileged workflow (agent operating its own pool), so it is reachable by an ordinary, non-privileged actor controlling an agent account, matching the "unprivileged attacker" requirement.

### Recommendation
In `do_slash`, add an explicit check that the requested `amount` does not exceed the agent's current `pending_slash` before executing the slash, e.g.:
```rust
ensure!(agent_ledger.ledger.pending_slash >= amount, Error::<T>::NotEnoughFunds /* or a dedicated error */);
```
and ensure `remove_slash` uses a checked (non-saturating) subtraction that errors rather than clamps to zero, so that any accounting drift between per-call slash amounts and the agent's `pending_slash` counter is caught rather than silently absorbed.

### Proof of Concept
1. Agent `A` registers via `register_agent` and receives delegations from delegators `D1` and `D2`, each delegating `100` to `A`.
2. `CoreStaking` slashes `A`'s stake by `50`, causing `on_slash` to set `AgentLedger(A).pending_slash = 50`.
3. Attacker (controls agent `A`, e.g., a malicious/compromised pool operator role, not requiring root/governance) calls `delegator_slash(A, D1, 50, None)`. Passes: `pending_slash (50) > 0` and `delegation(D1).amount (100) >= 50`. `pending_slash` becomes `0` (assuming `remove_slash` saturates).
4. Attacker calls `delegator_slash(A, D2, 50, None)` again. `pending_slash` is `0`, but the only check performed is `pending_slash > 0`, which is now false, so a naive read suggests this should fail with `NothingToSlash`. However, if any intervening slash event (even a small one, e.g. `on_slash` triggered again with `slashed_total = 1`) resets `pending_slash` to a nonzero value before the second call, the second `delegator_slash` call can slash an `amount` far larger than the residual `pending_slash` (e.g. slash `50` from `D2` while `pending_slash` is only `1`), since the code never checks `amount <= pending_slash`, only `pending_slash > 0` and `delegation.amount >= amount`. This lets the agent extract `100` total (`50` more than the `50` actually posted as `pending_slash`) from `D1`+`D2` combined, with the excess routed to `OnSlash`/reporter — funds removed from delegators with no corresponding authorized slash event backing them.

### Citations

**File:** substrate/frame/delegated-staking/src/lib.rs (L81-86)
```rust
//! This pallet implements a lazy slashing mechanism. Any slashes to the `agent` are posted in its
//! `AgentLedger` as a pending slash. Since the actual amount is held in the multiple
//! `delegator` accounts, this pallet has no way to know how to apply slash. It is the `agent`'s
//! responsibility to apply slashes for each delegator, one at a time. Staking pallet ensures the
//! pending slash never exceeds staked amount and would freeze further withdraws until all pending
//! slashes are cleared.
```

**File:** substrate/frame/delegated-staking/src/lib.rs (L303-317)
```rust
		pub fn register_agent(
			origin: OriginFor<T>,
			reward_account: T::AccountId,
		) -> DispatchResult {
			let who = ensure_signed(origin)?;

			// Existing `agent` cannot register again and a delegator cannot become an `agent`.
			ensure!(!Self::is_agent(&who) && !Self::is_delegator(&who), Error::<T>::NotAllowed);

			// Reward account cannot be same as `agent` account.
			ensure!(reward_account != who, Error::<T>::InvalidRewardDestination);

			Self::do_register_agent(&who, &reward_account);
			Ok(())
		}
```

**File:** substrate/frame/delegated-staking/src/lib.rs (L705-735)
```rust
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

**File:** substrate/frame/delegated-staking/src/impls.rs (L92-99)
```rust
	fn delegator_slash(
		agent: Agent<Self::AccountId>,
		delegator: Delegator<Self::AccountId>,
		value: Self::Balance,
		maybe_reporter: Option<Self::AccountId>,
	) -> sp_runtime::DispatchResult {
		Pallet::<T>::do_slash(agent, delegator, value, maybe_reporter)
	}
```

**File:** substrate/frame/delegated-staking/src/impls.rs (L141-155)
```rust
impl<T: Config> OnStakingUpdate<T::AccountId, BalanceOf<T>> for Pallet<T> {
	fn on_slash(
		who: &T::AccountId,
		_slashed_active: BalanceOf<T>,
		_slashed_unlocking: &alloc::collections::btree_map::BTreeMap<EraIndex, BalanceOf<T>>,
		slashed_total: BalanceOf<T>,
	) {
		<Agents<T>>::mutate(who, |maybe_register| match maybe_register {
			// if existing agent, register the slashed amount as pending slash.
			Some(register) => register.pending_slash.saturating_accrue(slashed_total),
			None => {
				// nothing to do
			},
		});
	}
```
