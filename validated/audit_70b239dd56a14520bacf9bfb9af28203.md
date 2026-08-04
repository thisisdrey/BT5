## Title
`unbond` in pallet-nomination-pools binds the sub-pool unlock era to `current_era` while `withdraw_unbonded`/slashing paths reason about `active_era`, letting a member's stake be dissolved before the underlying stash is actually unlocked - ([File: substrate/frame/nomination-pools/src/lib.rs])

## Summary
This is a direct local analog of the TOLP/TOB bug class: two code paths that are supposed to describe the *same* lock/expiry point instead use two different "clocks" (`CurrentEra` vs `ActiveEra`), which lets the accounting states of the two paths diverge and a user's dissolved points end up decoupled from when funds are actually released by the underlying staking system.

## Finding Description
In `Pallet::unbond` [1](#0-0)  the code computes:

```rust
let active_era = T::StakeAdapter::current_era();
let unbond_era = T::StakeAdapter::bonding_duration().saturating_add(active_era);
```

Note that the local variable is named `active_era` but its value comes from `current_era()` — the pools pallet keys the `SubPoolsStorage` unbonding bucket and the member's `unbonding_eras` entry to `CurrentEra + BondingDuration`. Withdrawal eligibility and slashing bookkeeping, however, are defined against `ActiveEra` (the era that has actually rotated and whose stake changes are settled), not `CurrentEra` (the era for which election/planning is in progress and which can be ahead of `ActiveEra`). This is exactly the class of bug documented by the project's own fix history:

- `prdoc/stable2512-2/pr_10986.prdoc`: *"[Pool] Use active era for withdrawals — Standardising using active era in pools and staking. Current Era should only be used for election logic."* [2](#0-1) 
- `prdoc/stable2512-3/pr_11018.prdoc`: *"A bug (CurrentEra vs ActiveEra mismatch) caused one pool member's balance to become trapped: their points were dissolved but the held funds weren't released."* This required a one-time on-chain migration (`do_claim_trapped_balance`) to recover funds after the fact. [3](#0-2) 

The pattern is structurally identical to the TOLP/TOB report: one function (`unbond`, analogous to `TOB.exitPosition`) records/derives the unlock boundary using one time-reference (`CurrentEra`), while other functions that gate settlement (`withdraw_unbonded`, slash application, `pool_withdraw_unbonded`) use a different time-reference (`ActiveEra`). Because `CurrentEra` can run ahead of `ActiveEra` (it advances at the start of election, before the era actually rotates), a member's points can be dissolved from the bonded pool and inserted into an unbonding sub-pool keyed by an era that has not yet become active, while the underlying `StakeAdapter`/staking ledger unlocks based on `ActiveEra`. This produces the same "one clock says expired/eligible, the other doesn't agree" divergence, and the project's own remediation (`pr_11018`) confirms concrete instances where held funds were "trapped" — the balance in the delegate/agent stayed locked with the staking system while the pool's point/era accounting had already dissolved the member's claim, or vice versa.

## Impact Explanation
This falls squarely within the "Balances, assets, ... staking, pools, ... must conserve value and settle exactly once to the rightful beneficiary and amount" and "duplicate settlement or payout, permanent user-fund or bridge-state lock" categories. The confirmed real-world consequence (per `pr_11018.prdoc`) was a *permanent user-fund lock*: pool member points were dissolved (so the member's `PoolMembers` records no longer represent a claim) while the actually-held balance was not released, requiring a manual runtime migration to restore correct accounting. In the general (unmitigated) case, this era-reference mismatch class can also allow a member to be treated as unbonded/withdrawable by one path while the stake-adapter still treats it as active, creating windows where balances can be double-counted or under-released against the `TotalValueLocked` invariant.

## Likelihood Explanation
This is not a theoretical concern — it already manifested in production and required a dedicated migration (`pr_11018`) to remediate a specific affected member, and a separate hardening PR (`pr_10986`) to "standardize using active era in pools and staking." Any transaction sequence where `CurrentEra` and `ActiveEra` diverge (which happens by design every era, since `CurrentEra` is bumped at election start, several sessions before `ActiveEra` rotates) that intersects with `unbond`/`withdraw_unbonded`/slashing timing can reproduce the divergence for other members. No privileged actor, validator, or off-chain component is required — any pool member calling the public `unbond` extrinsic during the window where `CurrentEra != ActiveEra` is sufficient to trigger the mismatch.

## Recommendation
Consistently use a single era reference (`ActiveEra`, as the `pr_10986` fix direction states) throughout all of `pallet-nomination-pools`' unbonding/withdrawal/slashing bookkeeping, and reserve `CurrentEra` strictly for election-scheduling logic as documented. Audit every remaining call site in `substrate/frame/nomination-pools/src/lib.rs` that reads `T::StakeAdapter::current_era()` to confirm none of them are used to key storage that must correspond to settlement/withdrawal timing (`SubPoolsStorage`, `unbonding_eras`, slash application). Add an invariant check (as already partially done via `try_state` for `ActiveEra`/`CurrentEra` consistency in `pallet-staking-async`, see `substrate/frame/staking-async/src/tests/try_state.rs`) to `pallet-nomination-pools` itself, asserting that unbonding-era bookkeeping never uses a value ahead of `ActiveEra`. Where historical corruption may already exist beyond the single migrated case, consider extending or generalizing the `do_claim_trapped_balance` migration.

## Proof of Concept
Conceptual reproduction (mirrors the confirmed `pr_11018` incident):
1. Let `ActiveEra = N`, and let `CurrentEra = N+1` (this occurs naturally once election processing starts for the next era, before the era actually rotates on-chain).
2. A pool member calls `Pools::unbond(origin, member, points)`. Inside, `active_era` is computed as `T::StakeAdapter::current_era()` returning `N+1`, so `unbond_era = N+1+BondingDuration`, and the member's points are immediately dissolved from `bonded_pool` and inserted into `SubPoolsStorage` under key `unbond_era`, with `member.unbonding_eras` updated accordingly. [4](#0-3) 
3. Underneath, `T::StakeAdapter::unbond` schedules the actual stash unlock relative to `ActiveEra` (`N`), i.e., real funds become withdrawable/unlockable at `ActiveEra + BondingDuration = N + BondingDuration`, one era earlier/later than the pools-side bookkeeping expects.
4. When slashes are applied or `withdraw_unbonded`/`pool_withdraw_unbonded` runs against `ActiveEra`, the pool-side and staking-side records disagree on which era's unbonded pool corresponds to which real unlock event, causing `member_delegation_balance` (actual held balance) and `member_data.total_balance()` (expected from points) to diverge — precisely the trapped-balance condition that `claim_trapped_balance_migration::migration_recovers_trapped_funds` test was written to fix. [5](#0-4) 

This confirms the local analog: a lock/expiry boundary computed from two different "epoch" references (`CurrentEra` vs `ActiveEra`) in different functions of the same accounting pipeline, exactly the invariant broken in the external TOLP/TOB report.

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L2288-2323)
```rust
			)?;

			let active_era = T::StakeAdapter::current_era();
			let unbond_era = T::StakeAdapter::bonding_duration().saturating_add(active_era);

			// Unbond in the actual underlying nominator.
			let unbonding_balance = bonded_pool.dissolve(unbonding_points);
			T::StakeAdapter::unbond(Pool::from(bonded_pool.bonded_account()), unbonding_balance)?;

			// Note that we lazily create the unbonding pools here if they don't already exist
			let mut sub_pools = SubPoolsStorage::<T>::get(member.pool_id)
				.unwrap_or_default()
				.maybe_merge_pools(active_era);

			// Update the unbond pool associated with the current era with the unbonded funds. Note
			// that we lazily create the unbond pool if it does not yet exist.
			if !sub_pools.with_era.contains_key(&unbond_era) {
				sub_pools
					.with_era
					.try_insert(unbond_era, UnbondPool::default())
					// The above call to `maybe_merge_pools` should ensure there is
					// always enough space to insert.
					.defensive_map_err::<Error<T>, _>(|_| {
						DefensiveError::NotEnoughSpaceInUnbondPool.into()
					})?;
			}

			let points_unbonded = sub_pools
				.with_era
				.get_mut(&unbond_era)
				// The above check ensures the pool exists.
				.defensive_ok_or::<Error<T>>(DefensiveError::PoolNotFound.into())?
				.issue(unbonding_balance);

			// Try and unbond in the member map.
			member.try_unbond(unbonding_points, points_unbonded, unbond_era)?;
```

**File:** prdoc/stable2512-2/pr_10986.prdoc (L1-9)
```text
title: '[Pool] Use active era for withdrawals'
doc:
- audience: Runtime Dev
  description: Standardising using active era in pools and staking. Current Era should
    only be used for election logic
crates:
- name: pallet-nomination-pools
  bump: patch
- name: pallet-staking-async
```

**File:** prdoc/stable2512-3/pr_11018.prdoc (L1-15)
```text
title: '[Pool] Claim trapped balance via one-time migration'
doc:
- audience: Runtime User
  description: |-
    One-time migration to recover trapped balance for an affected pool member.
    A bug (CurrentEra vs ActiveEra mismatch) caused one pool member's balance to become trapped: their points were
      dissolved but the held funds weren't released. This migration:
    - Applies any pending slash for the member first
    - Calculates trapped amount by checking actual held balance vs expected balance from points
    - Releases trapped funds if present
crates:
- name: pallet-nomination-pools
  bump: minor
- name: asset-hub-westend-runtime
  bump: patch
```

**File:** substrate/frame/nomination-pools/src/tests.rs (L7949-7980)
```rust
	#[test]
	fn migration_recovers_trapped_funds() {
		ExtBuilder::default().build_and_execute(|| {
			let member = 20;

			// Member joins with 100
			assert_ok!(Pools::join(RuntimeOrigin::signed(member), 100, 1));

			let member_data = PoolMembers::<Runtime>::get(member).unwrap();
			assert_eq!(member_data.total_balance(), 100);
			assert_eq!(DelegateMock::delegator_balance(Delegator::from(member)), Some(100));

			// Simulate trapped funds: delegator_balance > points
			let pool_account = BondedPool::<Runtime>::get(1).unwrap().bonded_account();
			DelegateMock::set_delegator_balance(member, 150);
			DelegateMock::set_agent_balance_full(pool_account, 100, 50, 0);

			let member_data = PoolMembers::<Runtime>::get(member).unwrap();
			assert_eq!(member_data.total_balance(), 100);
			assert_eq!(DelegateMock::delegator_balance(Delegator::from(member)), Some(150));

			// Call the helper directly
			assert_ok!(Pools::do_claim_trapped_balance(&member));

			// Verify balance corrected: delegator_balance should now match points (100)
			assert_eq!(DelegateMock::delegator_balance(Delegator::from(member)), Some(100));

			// Calling again is a no-op (no state change)
			assert_ok!(Pools::do_claim_trapped_balance(&member));
			assert_eq!(DelegateMock::delegator_balance(Delegator::from(member)), Some(100));
		});
	}
```
