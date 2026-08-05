### Title
`GlobalMaxCommission` reduction re-rates already-accrued pool rewards without snapshotting, misappropriating commission from payees to members - (File: `substrate/frame/nomination-pools/src/lib.rs`)

### Summary
`pallet-nomination-pools` already contains a patched instance of this exact bug class: `prdoc/pr_12397.prdoc` documents that `set_commission_max` used to force-lower a pool's `commission.current` via `try_update_max` without first calling `RewardPool::update_records`, letting rewards accrued at the old (higher) rate be re-rated at the new lower rate on the next `update_records` call, silently crediting the differential to members instead of the commission payee. That specific call site was fixed by adding an `update_records` snapshot immediately before `try_update_max` [1](#0-0) , mirroring `set_commission`'s ordering [2](#0-1) .

However, `Commission::current()` computes the *effective* commission as `min(self.current, GlobalMaxCommission)` [3](#0-2) , and `GlobalMaxCommission` is a single, pallet-wide storage value that can be lowered directly by `set_configs` via a plain `ConfigOp::Set` [4](#0-3) . This path performs no per-pool `update_records` snapshot at all — for every pool in the system, not just one. The exact same broken invariant that was fixed for `set_commission_max` (single pool) still exists, unbounded in scope, for `GlobalMaxCommission`.

### Finding Description
The pallet's own documentation states the invariant that must hold: "commission is applied to rewards based on the current commission in effect at the time rewards are transferred into the reward pool. This is to prevent the malicious behaviour of changing the commission rate to a very high value after rewards are accumulated, and thus claim an unexpectedly high chunk of the reward" [5](#0-4) .

`RewardPool::update_records` is the only mechanism that "closes the books" at a given commission rate before that rate changes — it is called before `try_update_current` in `set_commission` and (post-fix) before `try_update_max` in `set_commission_max`. But `set_configs`, the extrinsic that can lower `GlobalMaxCommission::<T>`, performs no iteration over `RewardPools` and no call to `update_records` for any pool:

```rust
config_op_exp!(GlobalMaxCommission::<T>, global_max_commission);
``` [6](#0-5) 

Since `Commission::current()` folds `GlobalMaxCommission` into its output on every read [7](#0-6) , lowering `GlobalMaxCommission` instantly changes the effective commission rate for **every pool whose raw `commission.current` exceeds the new cap**, retroactively re-rating reward-points that accrued since each pool's last `update_records` call. The next time anyone triggers `update_records` for such a pool (e.g. any member bonding/unbonding, or a `claim_payout`), the differential between the old (higher, pre-cap) rate and the new capped rate is computed over the *entire unswept reward interval*, and — following the same mechanics documented in `pr_12397.prdoc` — that differential is credited to members' reward points instead of the commission payee's `total_commission_pending`.

### Impact Explanation
This breaks intended pool accounting: value that rightfully belongs to a pool's commission payee (protected by the pallet's own commission-fairness invariant) is redirected to pool members whenever a global parameter is tuned. Because `GlobalMaxCommission` applies pallet-wide, a single `set_configs` call can misallocate commission across every affected pool in the system simultaneously — a systemic version of the exact bug that was deemed serious enough to patch for the single-pool `set_commission_max` case. This is a runtime accounting bug (value not settling to the rightful beneficiary), not a case of admin abuse: the root cause is that the pallet forgot to apply the same, already-established defensive pattern to this second influence path on `current()`.

### Likelihood Explanation
`GlobalMaxCommission` is documented as intended to be updated periodically ("intended to be updated only via governance") [8](#0-7) , so any pools with commission above a newly-lowered cap will be affected on every routine adjustment — no adversarial governance behavior is required, only a normal parameter change combined with any pool having accrued commission since its last snapshot, which is the common case in practice (pools rarely have `update_records` triggered on every single block).

### Recommendation
Before applying a lower `global_max_commission` in `set_configs`, iterate `BondedPools`/`RewardPools` and call `RewardPool::update_records` (using each pool's pre-change effective `commission.current()`) for every pool whose commission exceeds the new global max, exactly as is already done for the single-pool case in `set_commission` and `set_commission_max`. If iterating all pools is impractical, the amortized fix should be to record a "capped-since" checkpoint per pool and force a lazy `update_records` snapshot using the historical (pre-cap) rate at the first future write to a pool's reward pool after a `GlobalMaxCommission` reduction.

### Proof of Concept
Conceptual sequence (mirrors the already-confirmed `pr_12397` PoC pattern, generalized to the global path):
1. Pool `P` sets `commission.current = 20%` via `set_commission` (no `GlobalMaxCommission` set, or set high).
2. Rewards accrue into `P`'s bonded/reward account over several eras without anyone calling `claim_payout`/`bond`/`unbond` (no `update_records` snapshot taken).
3. Governance calls `set_configs` with `global_max_commission = ConfigOp::Set(5%)`. No `update_records` runs for `P` or any other pool.
4. A member of `P` calls `bond` or `claim_payout`, triggering `update_records`, which now computes commission at `min(20%, 5%) = 5%` over the entire unswept interval.
5. The differential accrued between 20% and 5% for that interval is credited to member reward points instead of `total_commission_pending`, so the commission payee permanently loses that portion of commission, and members are over-credited.

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L147-148)
```rust
//! If set, a pool's commission is bound to [`GlobalMaxCommission`] at the time it is applied to
//! pending rewards. [`GlobalMaxCommission`] is intended to be updated only via governance.
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L156-159)
```rust
//! Crucially, commission is applied to rewards based on the current commission in effect at the
//! time rewards are transferred into the reward pool. This is to prevent the malicious behaviour of
//! changing the commission rate to a very high value after rewards are accumulated, and thus claim
//! an unexpectedly high chunk of the reward.
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L843-850)
```rust
	/// Gets the pool's current commission, or returns Perbill::zero if none is set.
	/// Bounded to global max if current is greater than `GlobalMaxCommission`.
	fn current(&self) -> Perbill {
		self.current
			.as_ref()
			.map_or(Perbill::zero(), |(c, _)| *c)
			.min(GlobalMaxCommission::<T>::get().unwrap_or(Bounded::max_value()))
	}
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L2740-2767)
```rust
		pub fn set_configs(
			origin: OriginFor<T>,
			min_join_bond: ConfigOp<BalanceOf<T>>,
			min_create_bond: ConfigOp<BalanceOf<T>>,
			max_pools: ConfigOp<u32>,
			max_members: ConfigOp<u32>,
			max_members_per_pool: ConfigOp<u32>,
			global_max_commission: ConfigOp<Perbill>,
		) -> DispatchResult {
			T::AdminOrigin::ensure_origin(origin)?;

			macro_rules! config_op_exp {
				($storage:ty, $op:ident) => {
					match $op {
						ConfigOp::Noop => (),
						ConfigOp::Set(v) => <$storage>::put(v),
						ConfigOp::Remove => <$storage>::kill(),
					}
				};
			}

			config_op_exp!(MinJoinBond::<T>, min_join_bond);
			config_op_exp!(MinCreateBond::<T>, min_create_bond);
			config_op_exp!(MaxPools::<T>, max_pools);
			config_op_exp!(MaxPoolMembers::<T>, max_members);
			config_op_exp!(MaxPoolMembersPerPool::<T>, max_members_per_pool);
			config_op_exp!(GlobalMaxCommission::<T>, global_max_commission);

```

**File:** substrate/frame/nomination-pools/src/lib.rs (L2977-2988)
```rust
			let mut reward_pool = RewardPools::<T>::get(pool_id)
				.defensive_ok_or::<Error<T>>(DefensiveError::RewardPoolNotFound.into())?;
			// IMPORTANT: make sure that everything up to this point is using the current commission
			// before it updates. Note that `try_update_current` could still fail at this point.
			reward_pool.update_records(
				pool_id,
				bonded_pool.points,
				bonded_pool.commission.current(),
			)?;
			RewardPools::insert(pool_id, reward_pool);

			bonded_pool.commission.try_update_current(&new_commission)?;
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L3016-3029)
```rust
			let mut reward_pool = RewardPools::<T>::get(pool_id)
				.defensive_ok_or::<Error<T>>(DefensiveError::RewardPoolNotFound.into())?;
			// IMPORTANT: snapshot rewards accrued at the current commission before `try_update_max`
			// can force-lower it. Otherwise rewards accrued since the last snapshot would be
			// re-rated at the new (lower) rate and the differential credited to members instead
			// of the commission payee. Mirrors the ordering in `set_commission`.
			reward_pool.update_records(
				pool_id,
				bonded_pool.points,
				bonded_pool.commission.current(),
			)?;
			RewardPools::insert(pool_id, reward_pool);

			bonded_pool.commission.try_update_max(pool_id, max_commission)?;
```
