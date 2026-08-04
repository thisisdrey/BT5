I found a real local analog: `withdraw_unlocked` in the nomination pools pallet dissolves `unbonding_eras` entries whose era `<= active_era`, but this is decided purely by the pool member's locally recorded `unbonding_era`, independent of whether the underlying `pallet-staking`/`pallet-staking-async` ledger has actually released (`consolidate_unlocked`) the corresponding funds for that era. This is the exact class of bug described in `prdoc/stable2512-3/pr_11018.prdoc`, which documents that a `CurrentEra` vs `ActiveEra` mismatch caused a pool member's points to be dissolved while the held funds were never released — i.e., an internal accounting counter (member/pool points) advanced ahead of the real underlying balance movement, exactly mirroring the Puffer `lidoLockedETH` vs actual stETH-moved mismatch.

### Title
Pool member points can be dissolved before underlying staking ledger releases matching funds, trapping balance — (File: substrate/frame/nomination-pools/src/lib.rs)

### Summary
`Call::withdraw_unbonded` calls `member.withdraw_unlocked(active_era)`, which unconditionally removes any `unbonding_eras` entry whose era is `<= active_era` and treats the associated points as fully claimable via `sub_pools` dissolution — without any cross-check that `pallet-staking`'s ledger (`CurrentEra`/`ActiveEra`, `BondingDuration`, slashing spans) has actually unlocked and transferred an equivalent amount of real balance out of the bonded/delegated stash for that era.

### Finding Description
`PoolMember::withdraw_unlocked` (`substrate/frame/nomination-pools/src/lib.rs:668-687`) purely inspects the member's local `unbonding_eras` map and the caller-supplied `active_era` value: [1](#0-0) 

`active_era` is read from `T::StakeAdapter::current_era()` inside `withdraw_unbonded` at call time, but the actual balance movement out of the pool's bonded/delegated account is computed later and separately, via `sub_pools` dissolution and `T::StakeAdapter::transferable_balance`/`member_withdraw`: [2](#0-1) 

This is a two-step, non-atomic accounting flow: (1) points are irrevocably removed from `unbonding_eras`/sub-pools based on the era comparison, and (2) the actual withdrawable amount is derived from `StakeAdapter`, capped by `transferable_balance`. If the staking backend's own era bookkeeping (`CurrentEra` vs `ActiveEra`) is out of sync at the moment of the call — exactly the root cause acknowledged in the project's own fix record — step (1) can dissolve points for an era the staking ledger has not actually consolidated/unlocked yet, while step (2)'s `min(...)` with `transferable_balance` silently caps the withdrawal below what the member's dissolved points represented. The member's points are gone permanently (dissolved from `SubPools`), but the corresponding balance was never released to them, exactly the "counter incremented/points dissolved without proportional real balance movement" bug class described in the Puffer report.

The project's own `prdoc/stable2512-3/pr_11018.prdoc` and the accompanying `do_claim_trapped_balance`/`ClaimTrappedBalance` migration (`substrate/frame/nomination-pools/src/lib.rs:3295-3356`, `substrate/frame/nomination-pools/src/migration.rs:239-325`) confirm this exact scenario occurred in production and had to be remediated by a one-off, hardcoded, single-account migration: [3](#0-2) [4](#0-3) 

Crucially, this migration only fixes the balance for one already-affected, statically-known account (`A: Get<T::AccountId>`); it does not correct the general `withdraw_unlocked`/`withdraw_unbonded` control flow that produced the mismatch in the first place, so any future occurrence of the same `CurrentEra`/`ActiveEra` desynchronization will again permanently trap a member's balance with no generic recovery path.

### Impact Explanation
This directly matches "permanent freezing of funds" / "runtime bugs that compromise intended behavior" in the impact gate: a pool member's points can be dissolved (an irreversible state transition) while the balance they represent remains locked in the pool's bonded/delegated stash, unreachable through any public call, because points — the only claim mechanism — have already been zeroed out. Unlike the one-off migration, there is no generic, permissionless remedy in the pallet for a member who is not the specific hardcoded account fixed by `pr_11018`.

### Likelihood Explanation
The trigger condition (`CurrentEra` briefly diverging from `ActiveEra` in the staking backend, e.g. during era transitions/election delays) is a runtime/timing condition, not an attacker action — matching "implementation bugs" rather than "malicious validator" exclusions. The bug has already manifested once in production per the changelog, confirming the underlying race is realistic and not merely theoretical.

### Recommendation
Make `withdraw_unlocked`'s point-dissolution conditional on a verified, atomic confirmation that the staking backend has actually unlocked the matching balance for that era (e.g., re-derive `active_era` from the same source used by `consolidate_unlocked`, or gate the dissolution on `StakeAdapter` confirming the amount before mutating `unbonding_eras`/`SubPools`). Additionally, replace the single-account `ClaimTrappedBalance` migration with a permissionless, generic `claim_trapped_balance`-style extrinsic (the underlying `do_claim_trapped_balance` logic already exists) so any future desync self-heals without requiring a runtime upgrade per affected account.

### Proof of Concept
Not independently reproducible from static analysis alone since it requires reproducing a live `CurrentEra`/`ActiveEra` desynchronization in `pallet-staking`/`pallet-staking-async`; the project's own regression evidence is the `pr_11018` migration and its `pre_upgrade`/`post_upgrade` trapped-balance checks: [5](#0-4) 
which demonstrate `expected_balance` (derived from `member.total_balance()`, i.e., from points) diverging from `actual_balance` (`StakeAdapter::member_delegation_balance`) for at least one real account, confirming the points-vs-real-balance divergence class is exploitable/occurring in this codebase.

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L668-686)
```rust
	fn withdraw_unlocked(
		&mut self,
		active_era: EraIndex,
	) -> BoundedBTreeMap<EraIndex, BalanceOf<T>, T::MaxUnbonding> {
		// NOTE: if only drain-filter was stable..
		let mut removed_points =
			BoundedBTreeMap::<EraIndex, BalanceOf<T>, T::MaxUnbonding>::default();
		self.unbonding_eras.retain(|e, p| {
			if *e > active_era {
				true
			} else {
				removed_points
					.try_insert(*e, *p)
					.expect("source map is bounded, this is a subset, will be bounded; qed");
				false
			}
		});
		removed_points
	}
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L2438-2505)
```rust
			let withdrawn_points = member.withdraw_unlocked(active_era);
			ensure!(!withdrawn_points.is_empty(), Error::<T>::CannotWithdrawAny);

			// Before calculating the `balance_to_unbond`, we call withdraw unbonded to ensure the
			// `transferable_balance` is correct.
			let stash_killed = T::StakeAdapter::withdraw_unbonded(
				Pool::from(bonded_pool.bonded_account()),
				num_slashing_spans,
			)?;

			// defensive-only: the depositor puts enough funds into the stash so that it will only
			// be destroyed when they are leaving.
			ensure!(
				!stash_killed || caller == bonded_pool.roles.depositor,
				Error::<T>::Defensive(DefensiveError::BondedStashKilledPrematurely)
			);

			if stash_killed {
				// Maybe an extra consumer left on the pool account, if so, remove it.
				if frame_system::Pallet::<T>::consumers(&pool_account) == 1 {
					frame_system::Pallet::<T>::dec_consumers(&pool_account);
				}

				// Note: This is not pretty, but we have to do this because of a bug where old pool
				// accounts might have had an extra consumer increment. We know at this point no
				// other pallet should depend on pool account so safe to do this.
				// Refer to following issues:
				// - https://github.com/paritytech/polkadot-sdk/issues/4440
				// - https://github.com/paritytech/polkadot-sdk/issues/2037
			}

			let mut sum_unlocked_points: BalanceOf<T> = Zero::zero();
			let balance_to_unbond = withdrawn_points
				.iter()
				.fold(BalanceOf::<T>::zero(), |accumulator, (era, unlocked_points)| {
					sum_unlocked_points = sum_unlocked_points.saturating_add(*unlocked_points);
					if let Some(era_pool) = sub_pools.with_era.get_mut(era) {
						let balance_to_unbond = era_pool.dissolve(*unlocked_points);
						if era_pool.points.is_zero() {
							sub_pools.with_era.remove(era);
						}
						accumulator.saturating_add(balance_to_unbond)
					} else {
						// A pool does not belong to this era, so it must have been merged to the
						// era-less pool.
						accumulator.saturating_add(sub_pools.no_era.dissolve(*unlocked_points))
					}
				})
				// A call to this transaction may cause the pool's stash to get dusted. If this
				// happens before the last member has withdrawn, then all subsequent withdraws will
				// be 0. However the unbond pools do no get updated to reflect this. In the
				// aforementioned scenario, this check ensures we don't try to withdraw funds that
				// don't exist. This check is also defensive in cases where the unbond pool does not
				// update its balance (e.g. a bug in the slashing hook.) We gracefully proceed in
				// order to ensure members can leave the pool and it can be destroyed.
				.min(T::StakeAdapter::transferable_balance(
					Pool::from(bonded_pool.bonded_account()),
					Member::from(member_account.clone()),
				));

			// this can fail if the pool uses `DelegateStake` strategy and the member delegation
			// is not claimed yet. See `Call::migrate_delegation()`.
			T::StakeAdapter::member_withdraw(
				Member::from(member_account.clone()),
				Pool::from(bonded_pool.bonded_account()),
				balance_to_unbond,
				num_slashing_spans,
			)?;
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L3295-3334)
```rust
	/// Claim trapped balance for a pool member.
	///
	/// In rare scenarios, pool members may have excess held balance that is not accounted
	/// for in their pool points. This can occur when points are incorrectly dissolved
	/// without releasing the corresponding held funds.
	///
	/// If the pool has any pending slash, it will be applied to the member first before
	/// claiming the trapped balance.
	///
	/// Safe to call multiple times or for non-existent members — returns `Ok(())` as a
	/// no-op when there is nothing to do.
	pub fn do_claim_trapped_balance(member_account: &T::AccountId) -> DispatchResult {
		ensure!(
			T::StakeAdapter::strategy_type() == adapter::StakeStrategyType::Delegate,
			Error::<T>::NotSupported
		);

		// Apply any pending slash first. Ignore NothingToSlash and PoolMemberNotFound
		// (member existence is validated below).
		match Self::do_apply_slash(member_account, None, false) {
			Ok(_) => {},
			Err(e)
				if e == Error::<T>::NothingToSlash.into() ||
					e == Error::<T>::PoolMemberNotFound.into() => {},
			Err(_) => {
				return Err(Error::<T>::Defensive(DefensiveError::SlashNotApplied).into());
			},
		};

		let member = match PoolMembers::<T>::get(member_account) {
			Some(m) => m,
			None => return Ok(()),
		};

		let expected_balance = member.total_balance();
		let actual_balance =
			T::StakeAdapter::member_delegation_balance(Member::from(member_account.clone()))
				.unwrap_or_default();

		let trapped_amount = actual_balance.saturating_sub(expected_balance);
```

**File:** substrate/frame/nomination-pools/src/migration.rs (L241-262)
```rust
	/// One-time migration to claim trapped balance for a specific pool member.
	///
	/// Generic over `T: Config` and `A: Get<T::AccountId>` where `A` provides the account
	/// of the affected member. If `A` does not have trapped balance, this is a no-op.
	pub struct ClaimTrappedBalance<T, A>(core::marker::PhantomData<(T, A)>);

	impl<T: Config, A: Get<T::AccountId>> OnRuntimeUpgrade for ClaimTrappedBalance<T, A> {
		fn on_runtime_upgrade() -> Weight {
			let member_account = A::get();
			match Pallet::<T>::do_claim_trapped_balance(&member_account) {
				Ok(()) => {
					log!(info, "Successfully claimed trapped balance for {:?}", member_account);
				},
				Err(e) => {
					log!(info, "No trapped balance to claim for {:?}: {:?}", member_account, e);
				},
			}

			// Worst case: slash applied + trapped balance withdrawn.
			T::WeightInfo::apply_slash()
				.saturating_add(T::WeightInfo::withdraw_unbonded_update(T::MaxUnbonding::get()))
		}
```

**File:** substrate/frame/nomination-pools/src/migration.rs (L264-322)
```rust
		#[cfg(feature = "try-runtime")]
		fn pre_upgrade() -> Result<Vec<u8>, TryRuntimeError> {
			let member_account = A::get();
			let expected = PoolMembers::<T>::get(&member_account)
				.map(|m| m.total_balance())
				.unwrap_or_default();
			let actual =
				T::StakeAdapter::member_delegation_balance(Member::from(member_account.clone()))
					.unwrap_or_default();

			log!(
				info,
				"pre_upgrade: member {:?}, expected_balance: {:?}, actual_balance: {:?}, \
				 trapped: {:?}",
				member_account,
				expected,
				actual,
				actual.saturating_sub(expected)
			);

			Ok((expected, actual).encode())
		}

		#[cfg(feature = "try-runtime")]
		fn post_upgrade(data: Vec<u8>) -> Result<(), TryRuntimeError> {
			let member_account = A::get();
			let (pre_expected, pre_actual): (BalanceOf<T>, BalanceOf<T>) =
				Decode::decode(&mut &data[..])
					.map_err(|_| TryRuntimeError::Other("Failed to decode pre_upgrade data"))?;

			let post_actual =
				T::StakeAdapter::member_delegation_balance(Member::from(member_account.clone()))
					.unwrap_or_default();

			let post_expected = PoolMembers::<T>::get(&member_account)
				.map(|m| m.total_balance())
				.unwrap_or_default();

			log!(
				info,
				"post_upgrade: member {:?}, pre_expected: {:?}, pre_actual: {:?}, \
				 post_expected: {:?}, post_actual: {:?}",
				member_account,
				pre_expected,
				pre_actual,
				post_expected,
				post_actual
			);

			// If there was trapped balance before, it should now be resolved
			if pre_actual > pre_expected {
				ensure!(
					post_actual == post_expected,
					TryRuntimeError::Other("Trapped balance was not fully claimed after migration")
				);
			}

			Ok(())
		}
```
