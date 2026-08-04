## Analysis

The Solidity report's core broken invariant is: **work that was legitimately performed and recorded gets silently discarded from reward accounting because the reward function filters by "currently active" status rather than "active at the time the work was done."** The exact same pattern exists in `polkadot/runtime/parachains/src/reward_points.rs`.

### Local Analog

`RewardValidatorsWithEraPoints::reward_only_active` [1](#0-0)  resolves validator indices using the historical session's `AccountKeys` (correctly addressing the validator who actually did the work in that session), but then filters the resulting reward set against `C::ValidatorSet::validators()` — the **current, present-time** active set — before calling `R::reward_by_ids(rewards)`:

```rust
let validators = session_info::AccountKeys::<C>::get(&session_index);
...
let active_set: BTreeSet<_> = C::ValidatorSet::validators().into_iter().collect();
let rewards = indices.into_iter()
    .filter_map(|i| validators.get(i.0 as usize).cloned())
    .filter(|v| active_set.contains(v))
    .map(|v| (v, points));
R::reward_by_ids(rewards);
```

This is invoked from two call sites:
- `reward_backing`, called from inclusion when a backed candidate is included [2](#0-1) 
- `reward_dispute_statement`, called from `disputes.rs` after a dispute statement set is imported on-chain [3](#0-2) 

Disputes are explicitly designed to span multiple sessions (governed by `dispute_period`, and statements can be imported later as long as they're within `dispute_post_conclusion_acceptance_period`) [4](#0-3) . A validator can correctly back a candidate or cast a dispute vote while active in session `S`, but by the time the statement is actually processed on-chain and `reward_dispute_statement`/`reward_backing` executes, that validator may have rotated out of `C::ValidatorSet::validators()` (via `chill`, non-re-election, etc.). Because the filter checks the *current* active set instead of the active set of session `S` (which is already stored and available in `SessionInfo` at `Sessions::<T>::get(session)` [5](#0-4) ), the era points for a fully legitimate action are simply dropped — never redistributed, never credited to anyone, permanently lost — exactly mirroring the gauge losing its accumulated rewards after being deactivated.

### Title
Validators lose earned era-point rewards for legitimate backing/dispute-vote work performed before being rotated out of the active validator set - (File: polkadot/runtime/parachains/src/reward_points.rs)

### Summary
`reward_only_active` in `reward_points.rs` computes era-point rewards for backing and dispute-statement participation by resolving validator identities from the historical session where the work occurred, but then gates the actual credit on membership in the *current* active validator set rather than the active set of that historical session. Any validator that legitimately performed the on-chain-verified work (backing an included candidate, or casting a valid dispute vote) and subsequently exits the active set before the reward call executes has their era points silently and permanently discarded.

### Finding Description
`session_info::AccountKeys::<C>::get(&session_index)` correctly maps `ValidatorIndex` to `AccountId` for the session in which the backing/dispute activity happened [6](#0-5) . However the subsequent filter uses `C::ValidatorSet::validators()`, which returns the validator set active *now* (at call time), not the set active during `session_index` [7](#0-6) . Validators filtered out are excluded from `R::reward_by_ids(rewards)` entirely — there is no fallback, redistribution, or deferred crediting; the points for that validator for that action are gone.

For dispute votes specifically, this window can be substantial: dispute resolution and on-chain statement import can occur many blocks/sessions after the original session in which the disputed candidate was backed and included, governed by `dispute_period` and `dispute_post_conclusion_acceptance_period` [4](#0-3) . `SessionInfo` for the relevant session (including its `active_validator_indices`) is already stored on-chain at the time the reward call executes [8](#0-7)  — the correct historical active set is available but is not used.

### Impact Explanation
Era points feed directly into `pallet-staking`/`pallet-staking-async` payouts (`ErasRewardPoints`, validator incentive weight/budget calculations) [9](#0-8) . Silently dropping legitimately earned points is a runtime bug that compromises the intended reward/incentive behavior of the chain: validators who correctly performed backing or dispute duties receive less (or zero) compensation purely due to unrelated, later voluntary or routine set-membership changes, with no attacker, admin, or malicious actor required — any normal `chill`/re-election cycle can trigger it.

### Likelihood Explanation
This occurs under ordinary operational conditions — a validator chilling or not being re-elected is routine, and disputes intentionally allow processing across several sessions. No malicious peer, validator, or governance action is required, satisfying the "unprivileged" and "runtime bug that compromises intended behavior" bar. The `defensive_proof` comment above the filter indicates the authors were aware of edge cases here but the "active-set-at-call-time" filtering logic itself is unguarded and unconditionally drops rewards.

### Recommendation
Filter `reward_only_active` against the active validator set recorded for `session_index` (available via `session_info::Sessions::<C>::get(session_index).active_validator_indices` / the `validators` list already fetched) instead of `C::ValidatorSet::validators()` at call time, so that rewards are attributed based on the validator's status at the time the work was performed, not at the time the reward happens to be processed.

### Proof of Concept
1. Validator `V` is part of the active set during session `S` and backs a candidate that gets included, or casts a valid dispute statement for a dispute opened in session `S`.
2. Before the corresponding `reward_backing`/`reward_dispute_statement` call executes (e.g., the dispute statement is imported several sessions later, near the edge of `dispute_period`), `V` calls `chill` or is not re-elected, removing `V` from `C::ValidatorSet::validators()`.
3. When `process_checked_dispute_data` (or backing-reward processing) runs and calls `RewardValidators::reward_dispute_statement`/`reward_backing`, `reward_only_active` resolves `V`'s account correctly from `AccountKeys` for session `S`, but the `active_set.contains(v)` check fails because `V` is no longer in the *current* set.
4. `V`'s era points for this legitimate action are dropped from `R::reward_by_ids` and never recorded anywhere — permanently lost, with no compensating credit to any other party.

### Citations

**File:** polkadot/runtime/parachains/src/reward_points.rs (L43-66)
```rust
	/// Reward validators in session with points, but only if they are in the active set.
	fn reward_only_active(
		session_index: SessionIndex,
		indices: impl IntoIterator<Item = ValidatorIndex>,
		points: u32,
	) {
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
	}
```

**File:** polkadot/runtime/parachains/src/reward_points.rs (L75-78)
```rust
	fn reward_backing(indices: impl IntoIterator<Item = ValidatorIndex>) {
		let session_index = shared::CurrentSessionIndex::<C>::get();
		Self::reward_only_active(session_index, indices, BACKING_POINTS);
	}
```

**File:** polkadot/runtime/parachains/src/disputes.rs (L1163-1167)
```rust
		// Reward statements.
		T::RewardValidators::reward_dispute_statement(
			session,
			summary.new_participants.iter_ones().map(|i| ValidatorIndex(i as _)),
		);
```

**File:** polkadot/runtime/parachains/src/configuration.rs (L774-800)
```rust
		#[pallet::call_index(14)]
		#[pallet::weight((
			T::WeightInfo::set_config_with_u32(),
			DispatchClass::Operational,
		))]
		pub fn set_dispute_period(origin: OriginFor<T>, new: SessionIndex) -> DispatchResult {
			ensure_root(origin)?;
			Self::schedule_config_update(|config| {
				config.dispute_period = new;
			})
		}

		/// Set the dispute post conclusion acceptance period.
		#[pallet::call_index(15)]
		#[pallet::weight((
			T::WeightInfo::set_config_with_block_number(),
			DispatchClass::Operational,
		))]
		pub fn set_dispute_post_conclusion_acceptance_period(
			origin: OriginFor<T>,
			new: BlockNumberFor<T>,
		) -> DispatchResult {
			ensure_root(origin)?;
			Self::schedule_config_update(|config| {
				config.dispute_post_conclusion_acceptance_period = new;
			})
		}
```

**File:** polkadot/runtime/parachains/src/session_info.rs (L169-191)
```rust
		// The validator set is guaranteed to be of the current session
		// because we delay `on_new_session` till the end of the block.
		let account_ids = T::ValidatorSet::validators();
		let active_account_ids = take_active_subset(&active_set, &account_ids);
		AccountKeys::<T>::insert(&new_session_index, &active_account_ids);

		// create a new entry in `Sessions` with information about the current session
		let new_session_info = SessionInfo {
			validators, // these are from the notification and are thus already correct.
			discovery_keys: take_active_subset_and_inactive(&active_set, &discovery_keys),
			assignment_keys: take_active_subset(&active_set, &assignment_keys),
			validator_groups: validator_groups.into(),
			n_cores,
			zeroth_delay_tranche_width,
			relay_vrf_modulo_samples,
			n_delay_tranches,
			no_show_slots,
			needed_approvals,
			active_validator_indices: active_set,
			random_seed,
			dispute_period,
		};
		Sessions::<T>::insert(&new_session_index, &new_session_info);
```

**File:** substrate/frame/staking-async/src/session_rotation.rs (L415-421)
```rust
	/// Add reward points to validators using their stash account ID.
	///
	/// As a side effect, accumulates `weight × points` into [`ErasSumWeightedPoints`] for the
	/// active era, where `weight` is the validator's [`ErasValidatorIncentiveWeight`]. This
	/// keeps the denominator of the weighted-points share up to date without iterating every
	/// validator at payout time.
	pub(crate) fn reward_active_era(
```
