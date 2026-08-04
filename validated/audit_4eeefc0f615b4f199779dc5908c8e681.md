### Title
Delegators have no direct withdrawal path — funds are permanently locked if the `Agent` never calls `release_delegation` - (File: substrate/frame/delegated-staking/src/lib.rs)

### Summary
`pallet-delegated-staking` holds delegator funds under `HoldReason::StakingDelegation` while an `Agent` account stakes them on the delegator's behalf. The only function that can move a delegator's held funds back to that delegator, `release_delegation`, is dispatched exclusively by the `Agent` (`ensure_signed(origin)` becomes the `Agent` identity passed into `do_release`) — there is no dispatchable that lets the `delegator` unilaterally reclaim their own held balance. This mirrors the `DAOfiV1Pair` bug class: a fund-moving primitive is gated entirely behind a single third-party address (`router` in DAOfi, `agent` here), and that address can be registered permissionlessly by "any account" with no vetting, so a non-cooperating or malfunctioning agent can permanently strand delegator funds.

### Finding Description
`register_agent` explicitly documents that "this function allows any account to become an agent" [1](#0-0) , and `delegate_to_agent` only checks `Self::is_agent(&agent)` before moving and holding the delegator's funds [2](#0-1) . Once delegated, the funds are held via `T::Currency::hold(&HoldReason::StakingDelegation.into(), &delegator, amount)` inside `do_delegate` [3](#0-2) .

The single path back to the delegator is `release_delegation`, whose doc comment states "Only agents can call this," and which derives the `Agent` identity directly from `ensure_signed(origin)` with no cross-check against which delegators the caller is actually responsible for beyond the `Delegation` record lookup performed deep inside `do_release`/`do_delegate` [4](#0-3) . There is no dispatchable, in this pallet, that allows a `delegator` to withdraw their own held balance directly — the pallet's own doc explicitly states this ordering/withdrawal responsibility is "up to the consumer of this pallet to implement" [5](#0-4) .

This is structurally identical to the reported bug class: a value-holding contract/pallet restricts the only redemption path to a designated intermediary account (`router`/`Agent`), that intermediary can be freely chosen/registered without validation, and if the intermediary is unresponsive, buggy, or acts adversarially (e.g., simply never submitting `release_delegation`, or an upstream consumer pallet with a bug that never triggers it), the delegator's held funds have no other exit and remain locked indefinitely.

### Impact Explanation
If the consuming runtime pallet (or an externally-controlled `Agent` account that satisfies `is_agent`) never calls `release_delegation` for a given delegator — whether due to a bug in the caller's logic, the agent account becoming unreachable/bricked, or intentional non-cooperation — the delegator's held stake becomes permanently unrecoverable through this pallet, since `Delegators` funds are only unlocked by `T::Currency::release`/transfer logic reachable exclusively from the agent-gated call. This is a permanent user-fund lock, matching the "permanent user-fund lock" impact category for the Polkadot SDK program.

### Likelihood Explanation
Any account can register as an `Agent` (`register_agent` has no admin/whitelist gate), and delegators choose to delegate to that agent via `delegate_to_agent`. No malicious/privileged actor is required beyond an ordinary user picking (or being routed by a runtime integration to) a non-cooperative or defectively-integrated `Agent`; the pallet's own documentation acknowledges withdrawal ordering/enforcement is left entirely to the consumer, which is a design gap rather than a hypothetical edge case.

### Recommendation
Add a delegator-triggered fallback withdrawal path (e.g., a `force_release`/`withdraw` callable by the `delegator` themselves after core-staking unbonding period has elapsed and `CoreStaking` reports the funds as free), rather than relying solely on the `Agent` cooperating. Alternatively, bind the agent identity used for release more strictly (verify at delegation time that the chosen agent implementation guarantees eventual release, e.g. via a runtime-level whitelist of trusted agent pallets) so that arbitrary, unvalidated accounts cannot become the sole gatekeeper of already-held delegator funds.

### Proof of Concept
1. Delegator `D` calls `delegate_to_agent(agent = A, amount = X)`. Funds `X` are placed on hold under `HoldReason::StakingDelegation` for `D` (`do_delegate`).
2. Account `A` satisfies `is_agent` (registered via permissionless `register_agent`), so the delegation succeeds and `X` is bonded to `CoreStaking` via `do_bond`.
3. `A` never calls `release_delegation(origin=A, delegator=D, amount, num_slashing_spans)` — either because `A` is unresponsive, is a buggy consumer pallet integration, or deliberately withholds cooperation.
4. `D` has no other dispatchable in `pallet-delegated-staking` to unlock or reclaim the held `X`; the funds remain held indefinitely, reproducing the DAOfi "router-gated fund lock" pattern in a Substrate/staking context.

### Citations

**File:** substrate/frame/delegated-staking/src/lib.rs (L49-52)
```rust
//! ### Withdrawal Management
//! Agent unbonding does not regulate ordering of consequent withdrawal for delegators. This is upto
//! the consumer of this pallet to implement in what order unbondable funds from
//! [`Config::CoreStaking`] can be withdrawn by the delegators.
```

**File:** substrate/frame/delegated-staking/src/lib.rs (L297-302)
```rust
		/// Implementation note: This function allows any account to become an agent. It is
		/// important though that accounts that call [`StakingUnchecked::virtual_bond`] are keyless
		/// accounts. This is not a problem for now since this is only used by other pallets in the
		/// runtime which use keyless account as agents. If we later want to expose this as a
		/// dispatchable call, we should derive a sub-account from the caller and use that as the
		/// agent account.
```

**File:** substrate/frame/delegated-staking/src/lib.rs (L368-387)
```rust
		/// Release previously delegated funds by delegator to origin.
		///
		/// Only agents can call this.
		///
		/// Tries to withdraw unbonded funds from `CoreStaking` if needed and release amount to
		/// `delegator`.
		pub fn release_delegation(
			origin: OriginFor<T>,
			delegator: T::AccountId,
			amount: BalanceOf<T>,
			num_slashing_spans: u32,
		) -> DispatchResult {
			let who = ensure_signed(origin)?;
			Self::do_release(
				Agent::from(who),
				Delegator::from(delegator),
				amount,
				num_slashing_spans,
			)
		}
```

**File:** substrate/frame/delegated-staking/src/lib.rs (L429-450)
```rust
		pub fn delegate_to_agent(
			origin: OriginFor<T>,
			agent: T::AccountId,
			amount: BalanceOf<T>,
		) -> DispatchResult {
			let delegator = ensure_signed(origin)?;

			// ensure delegator is sane.
			ensure!(
				Delegation::<T>::can_delegate(&delegator, &agent),
				Error::<T>::InvalidDelegation
			);

			// ensure agent is sane.
			ensure!(Self::is_agent(&agent), Error::<T>::NotAgent);

			// add to delegation.
			Self::do_delegate(Delegator::from(delegator), Agent::from(agent.clone()), amount)?;

			// bond the newly delegated amount to `CoreStaking`.
			Self::do_bond(Agent::from(agent), amount)
		}
```

**File:** substrate/frame/delegated-staking/src/lib.rs (L588-589)
```rust
		// try to hold the funds.
		T::Currency::hold(&HoldReason::StakingDelegation.into(), &delegator, amount)?;
```
