## Title
Delegated funds routed through `pallet-delegated-staking` are credited as validator "own" stake, letting a validator-operated agent capture the delegator-excluded `ValidatorSelfStake` incentive pot and the commission-favored own-stake reward split - ([File: substrate/frame/delegated-staking/src/lib.rs])

### Summary
`pallet-staking-async` splits era rewards into two pots: a normal staker-reward pot shared between validator and nominators, and a separate `ValidatorSelfStake` incentive pot that is explicitly documented as "validator-only; nominators do not receive from it" [1](#0-0)  The weight for that pot, and the favorable split of the normal pot, are both computed purely from a validator's `own` exposure balance [2](#0-1)  `pallet-delegated-staking` lets any account register as an `Agent`, receive delegations from arbitrary `Delegator` accounts, and bond that delegated balance directly into `CoreStaking` under the agent's own stash via `do_bond` [3](#0-2)  Because staking's `own` exposure is simply the stash's own bonded ledger balance, delegated funds bonded this way are indistinguishable from genuinely self-owned stake once they reach `pallet-staking-async`. This is exactly analogous to the reported bug class: a wrapper ("agent"/contract) that re-presents pooled/delegated third-party funds as the operator's own stake to bypass the intended discount applied to delegated stake.

### Finding Description
The staking reward model intentionally treats "own" stake and "delegated/nominated" stake differently:
- `calculate_staker_reward` gives the validator's `own` share of exposure a commission-free, favorable cut of the reward, with the remainder going to nominators [4](#0-3) 
- `calculate_validator_incentive_weight` computes a separate incentive-pot weight based solely on `self_stake`, and per the trait doc this `ValidatorSelfStake` pot is validator-only — nominators/delegators are not supposed to receive any of it [1](#0-0) [5](#0-4) 

The `self_stake`/`own` value fed into both calculations comes straight from `ErasStakersOverview::<T>::get(era, &stash).own`, i.e. the stash's own bonded ledger amount [6](#0-5) 

`pallet-delegated-staking` is explicitly designed so that "delegated funds will be locked in-place in user's account" while an `Agent` account stakes them on the delegator's behalf, and the pallet's own docs state the vision is to let "foreign MultiLocations (smart contracts or pallets on another chain) build their own pooled staking solutions" on top of it [7](#0-6)  `delegate_to_agent` only checks that the delegator isn't already delegating elsewhere and that the agent is registered — nothing prevents the `Agent` from also being a direct validator (as opposed to only nominating on behalf of a pool) [3](#0-2)  Once delegated, `do_bond` calls straight into `CoreStaking` to bond the amount under the Agent's own stash ledger, exactly like a self-funded `bond_extra` [8](#0-7) 

Consequently, if the `Agent` account validates directly (instead of using the funds purely to nominate through a pool), all delegated balance becomes part of that validator's `own` exposure in `pallet-staking-async`'s exposure snapshot, with no on-chain marker distinguishing it from genuine self-stake. This lets the delegated funds:
1. Receive the commission-free, favorably-split "own" reward share instead of being treated as `others`/nominator stake, and
2. Contribute to `calculate_validator_incentive_weight`'s `self_stake` argument, capturing a slice of the `ValidatorSelfStake` pot that is documented as exclusively for validators, not nominators/delegators.

This mirrors the external report precisely: a wrapper mechanism (here, `pallet-delegated-staking`'s `Agent`/pooling primitive) lets externally-owned funds be re-presented as the pool/validator operator's own stake, bypassing the intended lower-weight treatment for delegated capital.

### Impact Explanation
An operator can advertise a "staking service" where third parties delegate funds to the operator's `Agent` account, while the operator validates directly with that agent stash. All delegated capital is then counted as the validator's `own` stake for both the standard staking-reward split and the validator-only `ValidatorSelfStake` incentive pot. This misallocates rewards that are supposed to be reserved for genuine self-stake to a pool of externally-owned, delegated funds, understating true nominator/delegator stake and overstating validator self-stake system-wide. Because `OptimumSelfStake`/`HardCapSelfStake` incentive curves are meant to reward genuinely "skin-in-the-game" self-funded validators, this bypass defeats that economic design at protocol scale without needing any privileged actor — any account can register as an agent and accept delegations.

### Likelihood Explanation
Likelihood is moderate-to-high in an environment where `pallet-delegated-staking` is enabled alongside direct validation: the pallet is intentionally permissionless (`register_agent`/`delegate_to_agent` are plain signed extrinsics) and imposes no restriction tying an `Agent` to nomination-pool-only usage [3](#0-2)  Any unprivileged pair of accounts (agent + delegator) can reproduce the pattern with ordinary calls; no relayer, validator collusion, or governance action is required.

### Recommendation
Distinguish delegated-but-self-bonded stake from genuinely self-funded stake before it is counted toward `own` exposure and the `ValidatorSelfStake` incentive weight. Either (a) have `pallet-delegated-staking` reject delegation-to-agent when the agent is validating directly rather than acting purely as a pool nominator, or (b) have `pallet-staking-async`'s exposure/incentive computation subtract known delegated-and-held amounts (tracked by `DelegatedStaking::Delegators`/`Agents` storage) from `own` before feeding it into `calculate_validator_incentive_weight` and the own-stake reward split.

### Proof of Concept
1. Account `A` registers as a `DelegatedStaking::Agent` via `register_agent` [3](#0-2) .
2. Multiple third-party accounts call `delegate_to_agent(A, amount)`, each transferring locked balance under `A`'s agent ledger, which internally calls `do_bond(A, amount)` → bonds into `CoreStaking` under `A`'s stash [8](#0-7) .
3. `A` calls `validate()` directly on `pallet-staking-async` rather than only nominating via a pool.
4. At era end, `ErasStakersOverview::<T>::get(era, A).own` reflects all delegated balance as `A`'s own stake [6](#0-5) .
5. `calculate_validator_incentive_weight(overview_own)` computes `A`'s incentive weight using the full delegated total, granting `A` a share of the `ValidatorSelfStake` pot that should be unreachable by delegated funds [5](#0-4) , and `calculate_staker_reward` grants the favorable own-stake cut of the normal reward pot on the same inflated `own` value [4](#0-3) .

### Citations

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

**File:** substrate/frame/delegated-staking/src/lib.rs (L58-63)
```rust
//! This importantly allows clients of this pallet to build their own strategies for reward/slashes.
//! For example, an `agent` account can choose to first slash the reward pot before slashing the
//! delegators. Or part of the reward can go to an insurance fund that can be used to cover any
//! potential future slashes. The goal is to eventually allow foreign MultiLocations
//! (smart contracts or pallets on another chain) to build their own pooled staking solutions
//! similar to `NominationPools`.
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

**File:** substrate/frame/staking-async/src/reward.rs (L236-242)
```rust
	fn calculate_validator_incentive_weight(self_stake: BalanceOf<T>) -> BalanceOf<T> {
		let optimum = OptimumSelfStake::<T>::get();
		let cap = HardCapSelfStake::<T>::get();
		let slope_factor = SelfStakeSlopeFactor::<T>::get();

		incentive_weight::<BalanceOf<T>>(self_stake, optimum, cap, slope_factor)
	}
```

**File:** substrate/frame/staking-async/src/reward.rs (L244-261)
```rust
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
