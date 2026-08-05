Audit Report

## Title
Delegated funds bonded via `pallet-delegated-staking`'s `do_bond` become validator "own" stake with no restriction preventing the agent from validating, letting delegated capital capture the validator-only `ValidatorSelfStake` incentive pot - ([File: substrate/frame/delegated-staking/src/lib.rs])

## Summary
`pallet-delegated-staking::delegate_to_agent` bonds delegator funds directly into `CoreStaking` under the `Agent`'s own stash via `do_bond`, which calls `T::CoreStaking::virtual_bond` (first bond) or `bond_extra` (subsequent bonds) with no check that the agent is only nominating rather than validating. [1](#0-0)  Because `pallet-staking-async` derives a validator's `own` exposure straight from the stash's bonded ledger total, and both the commission-favored own-stake reward split and the validator-only `ValidatorSelfStake` incentive weight are computed from that same `own` value, an agent that validates directly (instead of nominating on behalf of a pool) can capture rewards intended exclusively for genuine self-stake. [2](#0-1) [3](#0-2) 

## Finding Description
`delegate_to_agent` only validates that the delegator isn't delegating elsewhere and that the agent is registered; it does not check the agent's staking status in `CoreStaking`. [4](#0-3)  `do_bond` unconditionally bonds the delegated amount into the agent's own `CoreStaking` ledger via `virtual_bond`/`bond_extra`, with no restriction preventing that stash from subsequently calling `validate()`. [1](#0-0) 

Critically, the pallet's own `do_try_state` invariant check explicitly documents the expected (but unenforced) constraint: "agent should be bonded and not validator" — i.e., an agent with nonzero delegated stake is expected to have `StakerStatus::Nominator` or `StakerStatus::Idle`, never `StakerStatus::Validator`. [5](#0-4)  This check runs only in test/try-runtime builds — it is not an `ensure!` guard inside `delegate_to_agent`, `do_bond`, or the staking pallet's `validate` extrinsic, so nothing in production dispatch logic actually prevents an agent from calling `validate()` after accepting delegations.

Once the agent validates, `ErasStakersOverview::<T>::get(era, &stash).own` reflects the full bonded ledger total (delegated + any genuinely self-funded amount), and this value feeds both:
1. `calculate_staker_reward`'s `validator_own_stake` parameter, granting the commission-free, favorable own-stake cut of the normal reward pot. [6](#0-5) 
2. `calculate_validator_incentive_weight`'s `self_stake` parameter, which determines the share of the `ValidatorSelfStake` pot — a pot explicitly documented as "validator-only; nominators do not receive from it." [7](#0-6) [8](#0-7) 

## Impact Explanation
This is a runtime bug that compromises intended reward-allocation behavior: delegated, externally-owned funds are misclassified as validator "own" stake, letting the agent capture (a) the commission-favored own-stake share of the normal staking reward pot and (b) a share of the `ValidatorSelfStake` incentive pot that is documented as reserved exclusively for genuine self-stake. This misallocates protocol-level incentive-pot rewards away from their intended recipients (validators with real skin-in-the-game) without requiring any privileged actor, defeating the economic design of `OptimumSelfStake`/`HardCapSelfStake` at scale.

## Likelihood Explanation
The exploit path requires only unprivileged, permissionless signed extrinsics: `register_agent`, `delegate_to_agent` (repeatable by any third party), and `validate()`. [9](#0-8) [4](#0-3)  No relayer, validator collusion, leaked keys, or governance action is needed, and the pallet's own try-state check confirms this exact scenario ("agent should be bonded and not validator") is a known but not runtime-enforced invariant. [5](#0-4) 

## Recommendation
Enforce the invariant that is currently only checked in `do_try_state` as a hard runtime guard: reject `delegate_to_agent`/`do_bond` when the agent's `CoreStaking::status` is `Validator`, and/or reject `validate()` when the stash has nonzero delegated balance in `pallet-delegated-staking::Agents`. Alternatively, have `pallet-staking-async` subtract known delegated-and-held amounts (tracked via `DelegatedStaking::Agents`) from `own` before it is fed into `calculate_validator_incentive_weight` and the own-stake reward split.

## Proof of Concept
1. Account `A` calls `register_agent` to become a `DelegatedStaking::Agent`. [9](#0-8) 
2. Third-party accounts call `delegate_to_agent(A, amount)`, each triggering `do_bond(A, amount)` which bonds the amount into `A`'s `CoreStaking` ledger via `virtual_bond`/`bond_extra`. [1](#0-0) 
3. `A` calls `pallet-staking-async::validate()` directly (nothing in `delegate_to_agent`/`do_bond`/`validate` blocks this).
4. At era end, `ErasStakersOverview::<T>::get(era, A).own` equals the full delegated total, and this value is passed into `calculate_staker_reward` and `calculate_validator_incentive_weight`, granting `A` the favorable own-stake reward split and a share of the validator-only `ValidatorSelfStake` pot on funds that were never `A`'s own. [2](#0-1) [3](#0-2) 
5. Running `DelegatedStaking::do_try_state` (or the full try-state suite in a test build) after step 3 would fail with "agent should be bonded and not validator," confirming the invariant is violated but not runtime-enforced. [5](#0-4)

### Citations

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

**File:** substrate/frame/delegated-staking/src/lib.rs (L549-561)
```rust
	/// Bond `amount` to `agent_acc` in [`Config::CoreStaking`].
	fn do_bond(agent_acc: Agent<T::AccountId>, amount: BalanceOf<T>) -> DispatchResult {
		let agent_ledger = AgentLedgerOuter::<T>::get(&agent_acc.get())?;

		let available_to_bond = agent_ledger.available_to_bond();
		defensive_assert!(amount == available_to_bond, "not expected value to bond");

		if agent_ledger.is_bonded() {
			T::CoreStaking::bond_extra(&agent_ledger.key, amount)
		} else {
			T::CoreStaking::virtual_bond(&agent_ledger.key, amount, agent_ledger.reward_account())
		}
	}
```

**File:** substrate/frame/delegated-staking/src/lib.rs (L780-794)
```rust
	fn check_delegates(
		ledgers: BTreeMap<T::AccountId, AgentLedger<T>>,
	) -> Result<(), sp_runtime::TryRuntimeError> {
		for (agent, ledger) in ledgers {
			let staked_value = ledger.stakeable_balance();

			if !staked_value.is_zero() {
				ensure!(
					matches!(
						T::CoreStaking::status(&agent).expect("agent should be bonded"),
						sp_staking::StakerStatus::Nominator(_) | sp_staking::StakerStatus::Idle
					),
					"agent should be bonded and not validator"
				);
			}
```

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L415-425)
```rust
		// Use the overview's own-stake (not the page's, which is zeroed on pages > 0)
		// so the calculator sees the full validator self-stake for reward computation.
		let overview_own =
			ErasStakersOverview::<T>::get(era, &stash).map(|o| o.own).unwrap_or_default();

		let reward_split = T::StakerRewardCalculator::calculate_staker_reward(
			validator_total_payout,
			validator_commission,
			overview_own,
			exposure.total(),
		);
```

**File:** substrate/frame/staking-async/src/reward.rs (L236-261)
```rust
	fn calculate_validator_incentive_weight(self_stake: BalanceOf<T>) -> BalanceOf<T> {
		let optimum = OptimumSelfStake::<T>::get();
		let cap = HardCapSelfStake::<T>::get();
		let slope_factor = SelfStakeSlopeFactor::<T>::get();

		incentive_weight::<BalanceOf<T>>(self_stake, optimum, cap, slope_factor)
	}

	fn calculate_staker_reward(
		validator_total_reward: BalanceOf<T>,
		validator_commission: Perbill,
		validator_own_stake: BalanceOf<T>,
		total_exposure: BalanceOf<T>,
	) -> sp_staking::StakerRewardResult<BalanceOf<T>> {
		let validator_commission_payout = validator_commission.mul_floor(validator_total_reward);
		let leftover = validator_total_reward.saturating_sub(validator_commission_payout);
		let validator_exposure_part = Perbill::from_rational(validator_own_stake, total_exposure);
		let validator_staking_payout = validator_exposure_part.mul_floor(leftover);
		let validator_payout = validator_staking_payout.saturating_add(validator_commission_payout);
		let nominator_payout = leftover.saturating_sub(validator_staking_payout);

		// Validator and nominator payout is exactly same as total reward.
		debug_assert_eq!(validator_payout + nominator_payout, validator_total_reward);

		sp_staking::StakerRewardResult { validator_payout, nominator_payout }
	}
```

**File:** substrate/primitives/staking/src/lib.rs (L767-780)
```rust
/// Handles two independent reward calculations:
///
/// 1. **Staker reward split** ([`Self::calculate_staker_reward`]) — determines how a validator's
///    staking reward is divided between the validator and their nominators.
///
/// 2. **Validator incentive weight** ([`Self::calculate_validator_incentive_weight`]) — determines
///    a validator's relative share of a separate validator incentive pot, based on self-stake. This
///    incentive pot is validator-only; nominators do not receive from it.
pub trait StakerRewardCalculator<Balance> {
	/// Compute a weight for this validator's share of the validator incentive pot.
	///
	/// Called once per validator during era planning. All validators' weights are summed, and
	/// each validator's incentive payout is proportional to `their_weight / total_weight`.
	fn calculate_validator_incentive_weight(self_stake: Balance) -> Balance;
```
