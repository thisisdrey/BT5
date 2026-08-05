## Finding

### Title
`set_staking_configs` can set `MinCommission` above `MaxCommission`, bypassing the cross-field invariant enforced by `set_min_commission`/`set_max_commission` — (File: `substrate/frame/staking-async/src/pallet/mod.rs`)

### Summary
This is a direct structural analog of the Lybra bug: two related bound parameters (`SafeCollateralRatio` vs `BadCollateralRatio` in Lybra ↔ `MaxCommission` vs `MinCommission` in `pallet-staking-async`) must always satisfy a fixed relationship (`min <= max`), and the codebase provides two *different* public entry points that mutate one side of the relationship — only one of which validates against the other side.

### Finding Description
`pallet-staking-async` maintains two independent storage items, `MinCommission<T>` and `MaxCommission<T>`, that together bound the commission a validator may set via `validate()`.

The dedicated extrinsics correctly cross-validate each other: [1](#0-0) 

and the paired `set_min_commission` enforces `CommissionTooHigh` against `MaxCommission`, as demonstrated by: [2](#0-1) 

However, `MinCommission<T>` can *also* be written through the batched `set_staking_configs` extrinsic, which performs **no cross-check whatsoever** against `MaxCommission`: [3](#0-2) 

The `config_op_exp!` macro unconditionally `put()`s the new `MinCommission` value with zero validation against the current `MaxCommission::<T>::get()`, exactly mirroring the Lybra pattern where `setBadCollateralRatio` failed to correctly bound itself against the sibling `vaultSafeCollateralRatio`.

### Impact Explanation
If `MinCommission` is pushed above `MaxCommission` through `set_staking_configs` (e.g., `MinCommission = 50%`, `MaxCommission = 10%`), no `Perbill` value can simultaneously satisfy both bounds. `Staking::validate` checks commission against `MaxCommission` (rejecting anything above it, per `Error::<T>::CommissionTooHigh` in `max_commission_rejects_validate_above_max`) while other code paths (e.g. `force_apply_min_commission`) enforce the `MinCommission` floor. With `Min > Max`, every validator attempting to call `validate()` with a fresh commission is stuck: any value ≥ `MinCommission` is rejected as exceeding `MaxCommission`, and any value ≤ `MaxCommission` is too low relative to `MinCommission`. This breaks validator set commission management network-wide until governance issues a corrective call — a runtime bug that compromises intended behavior of the staking system and can stall commission-related dispatches for the entire validator set.

### Likelihood Explanation
`set_staking_configs` is gated behind `ensure_root(origin)` (root-only), so this cannot be triggered by an unprivileged attacker directly. It requires a root/governance call — but unlike a pure "governance abuse" scenario, the underlying flaw is a **missing validation check** in the implementation itself (identical in nature to the Lybra finding, which was also root/DAO-gated yet still confirmed as valid because the code failed to enforce its own documented invariant). A single misconfigured or partially-updated `set_staking_configs` call (e.g. forgetting the current `MaxCommission` value, or a batched proposal executed out of the intended order relative to a separate `set_max_commission` call) is sufficient to desynchronize the two bounds — no malicious actor is required, only an incomplete/incorrect parameter in one legitimate call.

### Recommendation
Add the same cross-validation used in `set_min_commission`/`set_max_commission` to the `min_commission` branch of `set_staking_configs`:
```rust
if let ConfigOp::Set(new_min) = min_commission {
    ensure!(new_min <= MaxCommission::<T>::get(), Error::<T>::CommissionTooHigh);
}
```
so both entry points share a single source of truth for the invariant.

### Proof of Concept
1. Governance calls `Staking::set_max_commission(root, Perbill::from_percent(10))` — `MaxCommission = 10%`.
2. Governance calls `Staking::set_staking_configs(root, ..., min_commission: ConfigOp::Set(Perbill::from_percent(50)), ...)` — no check is performed, `MinCommission` becomes `50%`.
3. Now `MinCommission (50%) > MaxCommission (10%)`.
4. Any validator calling `Staking::validate(origin, ValidatorPrefs { commission: c, .. })` fails for every possible `c`: `c >= 50%` is rejected by the `MaxCommission` cap check (`CommissionTooHigh`/similar), and `c < 50%` violates the `MinCommission` floor enforced elsewhere (e.g. via `force_apply_min_commission`). Validator commission management is effectively bricked until governance issues another corrective `set_max_commission`/`set_staking_configs` call.

### Citations

**File:** substrate/frame/staking-async/src/pallet/mod.rs (L2630-2669)
```rust
		pub fn set_staking_configs(
			origin: OriginFor<T>,
			min_nominator_bond: ConfigOp<BalanceOf<T>>,
			min_validator_bond: ConfigOp<BalanceOf<T>>,
			max_nominator_count: ConfigOp<u32>,
			max_validator_count: ConfigOp<u32>,
			chill_threshold: ConfigOp<Percent>,
			min_commission: ConfigOp<Perbill>,
			max_staked_rewards: ConfigOp<Percent>,
			are_nominators_slashable: ConfigOp<bool>,
			chill_inactive_threshold: ConfigOp<u32>,
		) -> DispatchResult {
			ensure_root(origin)?;

			if let ConfigOp::Set(threshold) = chill_inactive_threshold {
				ensure!(
					threshold > 1 && threshold <= T::HistoryDepth::get(),
					Error::<T>::InvalidChillInactiveThreshold
				);
			}

			macro_rules! config_op_exp {
				($storage:ty, $op:ident) => {
					match $op {
						ConfigOp::Noop => (),
						ConfigOp::Set(v) => <$storage>::put(v),
						ConfigOp::Remove => <$storage>::kill(),
					}
				};
			}

			config_op_exp!(MinNominatorBond<T>, min_nominator_bond);
			config_op_exp!(MinValidatorBond<T>, min_validator_bond);
			config_op_exp!(MaxNominatorsCount<T>, max_nominator_count);
			config_op_exp!(MaxValidatorsCount<T>, max_validator_count);
			config_op_exp!(ChillThreshold<T>, chill_threshold);
			config_op_exp!(MinCommission<T>, min_commission);
			config_op_exp!(MaxStakedRewards<T>, max_staked_rewards);
			config_op_exp!(AreNominatorsSlashable<T>, are_nominators_slashable);
			config_op_exp!(ChillInactiveThreshold<T>, chill_inactive_threshold);
```

**File:** substrate/frame/staking-async/src/pallet/mod.rs (L3131-3141)
```rust
		/// Sets the maximum commission that validators can set.
		///
		/// The dispatch origin must be `T::AdminOrigin`.
		#[pallet::call_index(33)]
		#[pallet::weight(T::WeightInfo::set_max_commission())]
		pub fn set_max_commission(origin: OriginFor<T>, new: Perbill) -> DispatchResult {
			T::AdminOrigin::ensure_origin(origin)?;
			ensure!(new >= MinCommission::<T>::get(), Error::<T>::CommissionTooLow);
			MaxCommission::<T>::put(new);
			Ok(())
		}
```

**File:** substrate/frame/staking-async/src/tests/configs.rs (L144-168)
```rust
#[test]
fn max_commission_min_commission_invariant() {
	ExtBuilder::default().build_and_execute(|| {
		// GIVEN: MinCommission = 10%
		assert_ok!(Staking::set_min_commission(RuntimeOrigin::root(), Perbill::from_percent(10)));

		// WHEN/THEN: Cannot set max below min
		assert_noop!(
			Staking::set_max_commission(RuntimeOrigin::root(), Perbill::from_percent(5)),
			Error::<Test>::CommissionTooLow
		);

		// GIVEN: MaxCommission = 50%
		assert_ok!(Staking::set_max_commission(RuntimeOrigin::root(), Perbill::from_percent(50)));

		// WHEN/THEN: Cannot set min above max
		assert_noop!(
			Staking::set_min_commission(RuntimeOrigin::root(), Perbill::from_percent(51)),
			Error::<Test>::CommissionTooHigh
		);

		// Equal values are fine
		assert_ok!(Staking::set_min_commission(RuntimeOrigin::root(), Perbill::from_percent(50)));
	});
}
```
