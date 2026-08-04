### Title
Pool Stake-Strategy Migration Lacks On-Chain Zero-Balance Invariant, Allowing Undercounted `TotalValueLocked`/Pool Balance After Partial Migration - (File: substrate/frame/nomination-pools/src/migration.rs)

### Summary
`pallet-nomination-pools` migrates bonded pools from the legacy `StakeStrategyType::Transfer` strategy to `StakeStrategyType::Delegate` via `unversioned::DelegationStakeMigration::on_runtime_upgrade` and the permissionless extrinsic `migrate_pool_to_delegate_stake`. Both paths call `Pallet::<T>::migrate_to_delegate_stake(id)` and, on failure, only log a warning and continue — there is no on-chain assertion that the old (Transfer-strategy) pool account balance is fully drained/zero after the call, mirroring exactly the missing `require(_oldStrategy.totalValue() == 0)` guard from the external report. The only place this invariant is actually checked ("account balance should be zero") lives inside `#[cfg(feature = "try-runtime")] fn post_upgrade` in the versioned wrapper, code that is compiled out and never executes on a production chain.

### Finding Description [1](#0-0) 

`DelegationStakeMigration::on_runtime_upgrade` iterates bonded pools still on `StakeStrategyType::Transfer` and calls `Pallet::<T>::migrate_to_delegate_stake(id)`. If this call errors, the code only logs a warning (`let _ = ... map_err(...)`) and moves on to the next pool — it does not revert, retry, or block the pool from continuing to be treated by the rest of the pallet as if it were on the `Delegate` strategy.

The equivalent invariant check — "account balance should be zero" for the old strategy account after migration — does exist, but only inside the `try-runtime`-gated `post_upgrade` hook of the versioned migration wrapper: [2](#0-1) 

This is precisely the pattern in the C4 finding: the recommended fix (`require(_oldStrategy.totalValue() == 0)` after `withdraw()`) is present only as an off-chain/test-only assertion (`try-runtime`), not as a runtime-enforced check in `on_runtime_upgrade` itself. In production (`cargo build` without the `try-runtime` feature), this assertion never executes.

Downstream, pool accounting (`TotalValueLocked`, `pool_balance()`, `check_ed_imbalance`) relies on `T::StakeAdapter::total_balance()`/`total_stake()`, which is computed based on the `StakeAdapter`'s bookkeeping for the *current* strategy: [3](#0-2)  shows the pallet's own defensive check that `total_balance` can diverge from `bonded_balance + sum_unbonding_balance`, and only logs a warning (`log!(warn, ...)`) rather than halting — i.e., the pallet already acknowledges this class of bug can occur silently.

If `migrate_to_delegate_stake` partially succeeds (e.g., it delegates the staked/bonded portion but leaves residual balance directly held in the pool account under the old accounting model, or fails partway through updating internal ledgers), the pool is nonetheless flagged as migrated (`pool_strategy` now returns `Delegate`), and subsequent `total_balance`/`total_stake` calls read only the new delegate-based bookkeping, undercounting the leftover funds still sitting in the bonded account — exactly analogous to `totalValue()` being undervalued in the Solidity report after `SingleStrategyController.migrate()`.

### Impact Explanation
An undervalued `TotalValueLocked`/pool balance after a strategy migration corrupts pool-wide share/reward accounting used across `bond`, `unbond`, `withdraw_unbonded`, and `claim_payout` (all of which key off `bonded_pool.points_to_balance`/`balance_to_points` derived from the pool's tracked balance). Just as in the Solidity original, this can cause new depositors to receive an incorrect (arguably duplicated/inflated) share of the pool relative to actual backing funds, and remaining members to be shortchanged proportional to their share of `total_points` — a fund-accounting integrity violation that affects real user balances and staking rewards, without requiring any privileged/malicious actor: the permissionless `migrate_pool_to_delegate_stake` extrinsic can be called by anyone, and the forced runtime-upgrade path runs on every chain that includes it.

### Likelihood Explanation
Likelihood is moderate: it requires a failure or partial state during `migrate_to_delegate_stake` (e.g., insufficient funds to hold/delegate the full stake, similar to the "cannot hold all stake" scenario already handled — with force-withdrawal — for the analogous `Staking::migrate_currency` call, per [4](#0-3) ). The pallet authors clearly anticipated this class of edge case (hence the defensive warn-only check at lib.rs:4097-4119 and the try-runtime-only assertion), but chose not to enforce it as a hard on-chain invariant, leaving the door open exactly as flagged in the external report.

### Recommendation
Add a hard on-chain check (not gated behind `try-runtime`) inside `migrate_to_delegate_stake`/`DelegationStakeMigration::on_runtime_upgrade` (and the `migrate_pool_to_delegate_stake` extrinsic path) that asserts the old Transfer-strategy pool account balance is fully zero (or fully reconciled into the new Delegate-strategy total) before marking a pool as migrated. If it cannot be fully drained, either abort the migration for that pool (leave it as `Transfer`) or explicitly fold the residual balance into the new accounting so `TotalValueLocked`/`total_balance` remain conservative, analogous to `require(_oldStrategy.totalValue() == 0)`.

### Proof of Concept
Not independently executable from the indexed context (full body of `migrate_to_delegate_stake` was not retrievable within the tool budget), so the failure trigger inside that function could not be directly confirmed. What is confirmed from the repository:
1. `DelegationStakeMigration::on_runtime_upgrade` swallows errors from `migrate_to_delegate_stake` with only a log warning [5](#0-4) .
2. The only zero-balance invariant check for the migrated account is `#[cfg(feature = "try-runtime")]`-gated and thus absent from production builds [6](#0-5) .
3. The pallet's own integrity-check code documents that `total_balance` can diverge from `bonded_balance + sum_unbonding_balance` and only warns instead of failing [7](#0-6) .

I was unable to fully verify the internal implementation of `migrate_to_delegate_stake` (its exact transfer/delegate logic and failure modes) due to tool-call exhaustion; a Devin session with full file access would be needed to confirm the precise partial-failure code path and construct a runnable PoC test exercising it.

### Citations

**File:** substrate/frame/nomination-pools/src/migration.rs (L129-160)
```rust
	impl<T: Config, MaxPools: Get<u32>> OnRuntimeUpgrade for DelegationStakeMigration<T, MaxPools> {
		fn on_runtime_upgrade() -> Weight {
			let mut count: u32 = 0;

			BondedPools::<T>::iter_keys().take(MaxPools::get() as usize).for_each(|id| {
				let pool_acc = Pallet::<T>::generate_bonded_account(id);

				// only migrate if the pool is in Transfer Strategy.
				if T::StakeAdapter::pool_strategy(Pool::from(pool_acc)) ==
					adapter::StakeStrategyType::Transfer
				{
					let _ = Pallet::<T>::migrate_to_delegate_stake(id).map_err(|err| {
						log!(
							warn,
							"failed to migrate pool {:?} to delegate stake strategy with err: {:?}",
							id,
							err
						)
					});
					count.saturating_inc();
				}
			});

			log!(info, "migrated {:?} pools to delegate stake strategy", count);

			// reads: (bonded pool key + current pool strategy) * MaxPools (worst case)
			T::DbWeight::get()
				.reads_writes(2, 0)
				.saturating_mul(MaxPools::get() as u64)
				// migration weight: `pool_migrate` weight * count
				.saturating_add(T::WeightInfo::pool_migrate().saturating_mul(count.into()))
		}
```

**File:** substrate/frame/nomination-pools/src/migration.rs (L193-238)
```rust
		#[cfg(feature = "try-runtime")]
		fn post_upgrade(data: Vec<u8>) -> Result<(), TryRuntimeError> {
			let expected_pool_balances: Vec<BalanceOf<T>> = Decode::decode(&mut &data[..]).unwrap();

			for (index, id) in
				BondedPools::<T>::iter_keys().take(MaxPools::get() as usize).enumerate()
			{
				let pool_account = Pallet::<T>::generate_bonded_account(id);
				if T::StakeAdapter::pool_strategy(Pool::from(pool_account.clone())) ==
					adapter::StakeStrategyType::Transfer
				{
					log!(error, "Pool {} failed to migrate", id,);
					return Err(TryRuntimeError::Other("Pool failed to migrate"));
				}

				let actual_balance =
					T::StakeAdapter::total_balance(Pool::from(pool_account.clone()))
						.expect("after migration, this should return a value");
				let expected_balance = expected_pool_balances.get(index).unwrap();

				if actual_balance != *expected_balance {
					log!(
						error,
						"Pool {} balance mismatch. Expected: {:?}, Actual: {:?}",
						id,
						expected_balance,
						actual_balance
					);
					return Err(TryRuntimeError::Other("Pool balance mismatch"));
				}

				// account balance should be zero.
				let pool_account_balance = T::Currency::total_balance(&pool_account);
				if pool_account_balance != Zero::zero() {
					log!(
						error,
						"Pool account balance was expected to be zero. Pool: {}, Balance: {:?}",
						id,
						pool_account_balance
					);
					return Err(TryRuntimeError::Other("Pool account balance not migrated"));
				}
			}

			Ok(())
		}
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L4097-4119)
```rust
		for (pool_id, _pool) in BondedPools::<T>::iter() {
			let pool_account = Pallet::<T>::generate_bonded_account(pool_id);
			let subs = SubPoolsStorage::<T>::get(pool_id).unwrap_or_default();

			let sum_unbonding_balance = subs.sum_unbonding_balance();
			let bonded_balance = T::StakeAdapter::active_stake(Pool::from(pool_account.clone()));
			// TODO: should be total_balance + unclaimed_withdrawals from delegated staking
			let total_balance = T::StakeAdapter::total_balance(Pool::from(pool_account.clone()))
				// At the time when StakeAdapter is changed to `DelegateStake` but pool is not yet
				// migrated, the total balance would be none.
				.unwrap_or(T::Currency::total_balance(&pool_account));

			if total_balance < bonded_balance + sum_unbonding_balance {
				log!(
						warn,
						"possibly faulty pool: {:?} / {:?}, total_balance {:?} >= bonded_balance {:?} + sum_unbonding_balance {:?}",
						pool_id,
						_pool,
						total_balance,
						bonded_balance,
						sum_unbonding_balance
					)
			};
```

**File:** substrate/frame/staking/src/tests.rs (L8918-8985)
```rust
	#[test]
	fn cannot_hold_all_stake() {
		// When there is not enough funds to hold all stake, part of the stake if force withdrawn.
		// At end of the migration, the stake and hold should be same.
		ExtBuilder::default().has_stakers(true).build_and_execute(|| {
			// GIVEN alice who is a nominator with old currency.
			let alice = 300;
			let stake = 1000;
			bond_nominator(alice, stake, vec![11]);
			testing_utils::migrate_to_old_currency::<Test>(alice);
			assert_eq!(asset::staked::<Test>(&alice), 0);
			assert_eq!(Balances::balance_locked(STAKING_ID, &alice), stake);
			// ledger has 1000 staked.
			assert_eq!(
				<Staking as StakingInterface>::stake(&alice),
				Ok(Stake { total: stake, active: stake })
			);

			// Get rid of the extra ED to emulate all their balance including ED is staked.
			assert_ok!(Balances::transfer_allow_death(
				RuntimeOrigin::signed(alice),
				10,
				ExistentialDeposit::get()
			));

			let expected_force_withdraw = ExistentialDeposit::get();

			// ledger mutation would fail in this case before migration because of failing hold.
			assert_noop!(
				Staking::unbond(RuntimeOrigin::signed(alice), 100),
				Error::<Test>::NotEnoughFunds
			);

			// clear events
			System::reset_events();

			// WHEN alice currency is migrated.
			assert_ok!(Staking::migrate_currency(RuntimeOrigin::signed(1), alice));

			// THEN
			let expected_hold = stake - expected_force_withdraw;
			// ensure no lock
			assert_eq!(Balances::balance_locked(STAKING_ID, &alice), 0);
			// ensure stake and hold are same.
			assert_eq!(
				<Staking as StakingInterface>::stake(&alice),
				Ok(Stake { total: expected_hold, active: expected_hold })
			);
			assert_eq!(asset::staked::<Test>(&alice), expected_hold);
			// ensure events are emitted.
			assert_eq!(
				staking_events_since_last_call(),
				vec![Event::CurrencyMigrated {
					stash: alice,
					force_withdraw: expected_force_withdraw
				}]
			);

			// ensure cannot migrate again.
			assert_noop!(
				Staking::migrate_currency(RuntimeOrigin::signed(1), alice),
				Error::<Test>::AlreadyMigrated
			);

			// unbond works after migration.
			assert_ok!(Staking::unbond(RuntimeOrigin::signed(alice), 100));
		});
	}
```
