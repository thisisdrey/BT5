Audit Report

## Title
Backing/dispute-statement era-points forfeited when a validator is no longer in the *current* active set at reward time - ([File: polkadot/runtime/parachains/src/reward_points.rs])

## Summary
`RewardValidatorsWithEraPoints::reward_only_active` resolves validator identities using the *historical* session snapshot (`session_info::AccountKeys::<C>::get(&session_index)`) but filters those identities against `C::ValidatorSet::validators()`, which is the *current* active set at the time the reward call executes rather than the set active during `session_index`. This causes honest backing/dispute-statement era points to be silently dropped for validators who have since rotated out of the active set, before `R::reward_by_ids` is ever invoked, so the points are never credited to `era_rewards.individual` or `era_rewards.total`.

## Finding Description
`reward_only_active` in <cite repo="Kohvert/polkadot-sdk--029" path="polkadot/runtime/parachains/src/reward_points.rs" start="44-66" /> performs:
1. `validators = session_info::AccountKeys::<C>::get(&session_index)` — correct historical resolution for the session in which the qualifying action occurred.
2. `active_set: BTreeSet<_> = C::ValidatorSet::validators().into_iter().collect()` — the current live set at call time.
3. `.filter(|v| active_set.contains(v))` before `R::reward_by_ids(rewards)`. [1](#0-0) 

This is called from `reward_backing`, using `shared::CurrentSessionIndex::<C>::get()` [2](#0-1) , and from `reward_dispute_statement`, using an explicit, potentially older `session` parameter [3](#0-2) . Since dispute conclusion depends on collecting supermajority/byzantine threshold votes, this can span multiple sessions after the disputed candidate's original session, during which ordinary validator set rotation (elections, chilling, etc.) can remove the validator from `C::ValidatorSet::validators()` while they remain correctly resolvable historically via `AccountKeys`. In that scenario `active_set.contains(v)` evaluates false and the validator's reward record is filtered out entirely before reaching `R::reward_by_ids`, meaning the points are lost from both `era_rewards.individual` and `era_rewards.total` in the downstream `reward_by_ids`/`reward_active_era` implementations [4](#0-3) .

## Impact Explanation
This is a runtime correctness defect that compromises intended validator-incentive behavior: validators performing correct, honest, threshold-contributing work (backing a candidate or casting a dispute statement) can receive zero era points purely due to session-rotation timing outside their control, silently reducing their share of the era's staking payout with no error or event. This matches the "runtime bugs that compromise intended behavior" category in the impact gate.

## Likelihood Explanation
No privileged actor or malicious behavior is required — normal validator set churn (elections, chilling, slashing-driven removal) between the session in which a backing/dispute statement is cast and the later point at which the reward call executes is sufficient. For disputes specifically, the `session` parameter is explicitly decoupled from "now," making this divergence a designed possibility rather than an edge case requiring adversarial timing. That said, this is triggered by ordinary session-rotation/consensus-layer state, not by an unprivileged attacker's public extrinsic input — the reward call is invoked internally by the inclusion/disputes pallets as part of normal chain operation, not as a result of an attacker-controlled call. The bug is real and reproducible in code review, but it is a self-inflicted economic/incentive-ledger inaccuracy rather than an externally exploitable path to fund theft, duplicate settlement, unauthorized execution, or a chain-halting condition.

## Recommendation
Gate `reward_only_active` on the validator set that was active during `session_index` (consistent with how `AccountKeys::<C>::get(&session_index)` is resolved), for example by using `session_info::Sessions::<C>::get(&session_index).active_validator_indices` or an equivalent historical active-set snapshot, rather than `C::ValidatorSet::validators()` at call time.

## Proof of Concept
1. Validator `V` is part of the active set during session `N` and casts a valid dispute statement that contributes to resolving a dispute over a session-`N` candidate.
2. Several sessions pass; through ordinary rotation `V` is no longer in `C::ValidatorSet::validators()` (the current set) while still resolvable via `session_info::AccountKeys::<C>::get(N)`.
3. The dispute concludes later and `RewardValidators::reward_dispute_statement(N, validators)` is invoked, calling `reward_only_active(N, validators, DISPUTE_STATEMENT_POINTS)`.
4. `active_set.contains(v)` evaluates `false` for `V`, filtering `V` out of `rewards` before `R::reward_by_ids` is called — confirmed directly from the code at [5](#0-4) .
5. `V` receives no era-point credit for session `N`'s dispute vote, and the points are absent from both `individual` and `total` in `ErasRewardPoints`, permanently reducing `V`'s share of that era's payout with no error or corrective mechanism.

### Citations

**File:** polkadot/runtime/parachains/src/reward_points.rs (L49-65)
```rust
		let validators = session_info::AccountKeys::<C>::get(&session_index);
		let validators = match validators
			.defensive_proof("account_keys are present for dispute_period sessions")
		{
			Some(validators) => validators,
			None => return,
		};
		// limit rewards to the active validator set
		let active_set: BTreeSet<_> = C::ValidatorSet::validators().into_iter().collect();

		let rewards = indices
			.into_iter()
			.filter_map(|i| validators.get(i.0 as usize).cloned())
			.filter(|v| active_set.contains(v))
			.map(|v| (v, points));

		R::reward_by_ids(rewards);
```

**File:** polkadot/runtime/parachains/src/reward_points.rs (L75-78)
```rust
	fn reward_backing(indices: impl IntoIterator<Item = ValidatorIndex>) {
		let session_index = shared::CurrentSessionIndex::<C>::get();
		Self::reward_only_active(session_index, indices, BACKING_POINTS);
	}
```

**File:** polkadot/runtime/parachains/src/reward_points.rs (L89-94)
```rust
	fn reward_dispute_statement(
		session: SessionIndex,
		validators: impl IntoIterator<Item = ValidatorIndex>,
	) {
		Self::reward_only_active(session, validators, DISPUTE_STATEMENT_POINTS);
	}
```

**File:** substrate/frame/staking/src/pallet/impls.rs (L838-858)
```rust
	/// Add reward points to validators using their stash account ID.
	///
	/// Validators are keyed by stash account ID and must be in the current elected set.
	///
	/// For each element in the iterator the given number of points in u32 is added to the
	/// validator, thus duplicates are handled.
	///
	/// At the end of the era each the total payout will be distributed among validator
	/// relatively to their points.
	///
	/// COMPLEXITY: Complexity is `number_of_validator_to_reward x current_elected_len`.
	fn reward_by_ids(validators_points: impl IntoIterator<Item = (T::AccountId, u32)>) {
		if let Some(active_era) = ActiveEra::<T>::get() {
			<ErasRewardPoints<T>>::mutate(active_era.index, |era_rewards| {
				for (validator, points) in validators_points.into_iter() {
					*era_rewards.individual.entry(validator).or_default() += points;
					era_rewards.total += points;
				}
			});
		}
	}
```
