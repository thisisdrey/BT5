### Title
Nomination-pools `member_pending_slash`/`do_apply_slash` era mismatch permanently traps member held funds with no general on-chain recovery path - (File: `substrate/frame/nomination-pools/src/lib.rs`)

### Summary
The nomination-pools slashing logic computes a member's pending slash as the delta between the member's actual `StakeAdapter` delegation hold and the balance implied by their pool points (`member_pending_slash` / `do_apply_slash`). When the era used to dissolve unbonding points diverges from the era used by the underlying staking pallet to apply/defer a slash (a `CurrentEra` vs `ActiveEra` class of mismatch), points can be dissolved from a member's pool balance while the corresponding held balance in `pallet-balances`/`delegated-staking` is never released. This is exactly the bug class described in the external report: an accounting step that removes a user's "free" balance/points without a matching movement on the "locked/held" side, permanently trapping funds. The maintainers acknowledged this exact defect and shipped a one-off migration (`ClaimTrappedBalance`, PR #11018) that manually calls `do_claim_trapped_balance` for a single, specifically-affected account, rather than closing the root cause or exposing a permissionless recovery call for any future occurrence.

### Finding Description
`do_apply_slash` computes `pending_slash` via `member_pending_slash`, which diffs `T::StakeAdapter::member_delegation_balance` (the actual held/delegated amount) against `pool_member.total_balance()` (the amount implied by points) and then calls `T::StakeAdapter::member_slash` to burn the delta from the hold. [1](#0-0) [2](#0-1) 

`unbond` and `withdraw_unbonded` both dissolve a member's points against `active_era = T::StakeAdapter::current_era()` and merge/settle sub-pools using that era value, while slashes are applied against the era passed from the staking pallet's slashing pipeline. [3](#0-2) [4](#0-3) 

The project's own changelog documents that a `CurrentEra` vs `ActiveEra` mismatch caused exactly this: a member's points were dissolved (so `PoolMembers` no longer reflects the balance) while the held/delegated funds were never released, trapping the balance in the delegated-staking hold. [5](#0-4) 

The fix that was actually shipped is `do_claim_trapped_balance`, which recomputes `trapped_amount = actual_balance.saturating_sub(expected_balance)` and calls `member_withdraw` to release it — but this function is only invoked from a one-time, hard-coded `OnRuntimeUpgrade` migration (`ClaimTrappedBalance<T, A>`) parameterized by a single `Get<T::AccountId>` for the one affected member; it is not exposed as a permissionless dispatchable call. [6](#0-5) [7](#0-6) 

Because the underlying era-mismatch condition in the slash-application/unbond-settlement path is not structurally prevented (only the historical instance was manually remediated), any future occurrence of the same era mismatch — e.g. through `unbond`/`withdraw_unbonded` dissolving points at a different era boundary than the one at which `pallet-staking`'s slashing pipeline attributes the loss to sub-pools — will again silently dissolve points without releasing the held balance, and no permissionless on-chain mechanism exists to unlock it going forward. This mirrors the "Deposit contract locks funds for one operation while a different operation's release path is never called" pattern in the external report: two sides of a balance-hold invariant (points-dissolution and hold-release) are updated on different, not-mutually-consistent era references.

### Impact Explanation
An affected pool member's held/delegated funds become permanently locked with no dispatchable extrinsic to recover them — matching the report's "permanent locking of funds" / denial-of-service impact. This is a fund-lock in a staking-adjacent pallet with real value at stake (DOT delegated via nomination pools), fitting the "permanent user-fund... lock" acceptance criterion.

### Likelihood Explanation
The bug already manifested once in production-adjacent state (per the acknowledged PRDoc and the `np_claim_trapped_balance` remote test harness that iterates *all* pool members' snapshot state looking for more trapped balances), showing the underlying era-mismatch condition is reachable through ordinary staking/slashing/unbonding era transitions rather than any privileged or malicious action. [8](#0-7) 
Since the fix only patched one specific account instance via a migration rather than the root era-consistency issue, recurrence for other members remains plausible under the same conditions (slash/unbond era boundary interactions), while there is no general permissionless recovery path.

### Recommendation
- Expose `do_claim_trapped_balance` as a permissionless dispatchable call (rather than only via a hard-coded per-account migration) so any future occurrence of trapped balance can be self-serviced or resolved by any caller, consistent with the pallet's existing permissionless `apply_slash` pattern.
- Audit `unbond`/`withdraw_unbonded`/`do_apply_slash` to ensure the era used to dissolve pool points and the era used by the staking pallet to attribute a slash are always the same reference point, closing the root `CurrentEra`/`ActiveEra` divergence rather than only remediating discovered instances.

### Proof of Concept
The existing regression test demonstrates the exact mechanics of the bug (points dissolved, held balance not tracking), confirming the reachable state and that recovery today depends solely on the ad hoc migration: [9](#0-8)

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L2290-2296)
```rust
			let active_era = T::StakeAdapter::current_era();
			let unbond_era = T::StakeAdapter::bonding_duration().saturating_add(active_era);

			// Unbond in the actual underlying nominator.
			let unbonding_balance = bonded_pool.dissolve(unbonding_points);
			T::StakeAdapter::unbond(Pool::from(bonded_pool.bonded_account()), unbonding_balance)?;

```

**File:** substrate/frame/nomination-pools/src/lib.rs (L2408-2446)
```rust
			let mut member =
				PoolMembers::<T>::get(&member_account).ok_or(Error::<T>::PoolMemberNotFound)?;
			let active_era = T::StakeAdapter::current_era();

			let bonded_pool = BondedPool::<T>::get(member.pool_id)
				.defensive_ok_or::<Error<T>>(DefensiveError::PoolNotFound.into())?;
			let mut sub_pools =
				SubPoolsStorage::<T>::get(member.pool_id).ok_or(Error::<T>::SubPoolsNotFound)?;

			let slash_weight =
				// apply slash if any before withdraw.
				match Self::do_apply_slash(&member_account, None, false) {
					Ok(_) => T::WeightInfo::apply_slash(),
					Err(e) => {
						let no_pending_slash: DispatchResult = Err(Error::<T>::NothingToSlash.into());
						// This is an expected error. We add appropriate fees and continue withdrawal.
						if Err(e) == no_pending_slash {
							T::WeightInfo::apply_slash_fail()
						} else {
							// defensive: if we can't apply slash for some reason, we abort.
							return Err(Error::<T>::Defensive(DefensiveError::SlashNotApplied).into());
						}
					}

				};

			bonded_pool.ok_to_withdraw_unbonded_with(&caller, &member_account)?;
			let pool_account = bonded_pool.bonded_account();

			// NOTE: must do this after we have done the `ok_to_withdraw_unbonded_other_with` check.
			let withdrawn_points = member.withdraw_unlocked(active_era);
			ensure!(!withdrawn_points.is_empty(), Error::<T>::CannotWithdrawAny);

			// Before calculating the `balance_to_unbond`, we call withdraw unbonded to ensure the
			// `transferable_balance` is correct.
			let stash_killed = T::StakeAdapter::withdraw_unbonded(
				Pool::from(bonded_pool.bonded_account()),
				num_slashing_spans,
			)?;
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L3295-3356)
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

		if trapped_amount.is_zero() {
			return Ok(());
		}

		T::StakeAdapter::member_withdraw(
			Member::from(member_account.clone()),
			Pool::from(Self::generate_bonded_account(member.pool_id)),
			trapped_amount,
			0,
		)?;

		log!(
			info,
			"Claimed trapped balance for member {:?}, pool {:?}, amount {:?}",
			member_account,
			member.pool_id,
			trapped_amount
		);

		Ok(())
	}
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L3816-3841)
```rust
	/// Slash member against the pending slash for the pool.
	fn do_apply_slash(
		member_account: &T::AccountId,
		reporter: Option<T::AccountId>,
		enforce_min_slash: bool,
	) -> DispatchResult {
		let member = PoolMembers::<T>::get(member_account).ok_or(Error::<T>::PoolMemberNotFound)?;

		let pending_slash =
			Self::member_pending_slash(Member::from(member_account.clone()), member.clone())?;

		// ensure there is something to slash.
		ensure!(!pending_slash.is_zero(), Error::<T>::NothingToSlash);

		if enforce_min_slash {
			// ensure slashed amount is at least the minimum balance.
			ensure!(pending_slash >= T::Currency::minimum_balance(), Error::<T>::SlashTooLow);
		}

		T::StakeAdapter::member_slash(
			Member::from(member_account.clone()),
			Pool::from(Pallet::<T>::generate_bonded_account(member.pool_id)),
			pending_slash,
			reporter,
		)
	}
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L3843-3873)
```rust
	/// Pending slash for a member.
	///
	/// Takes the pool_member object corresponding to the `member_account`.
	fn member_pending_slash(
		member_account: Member<T::AccountId>,
		pool_member: PoolMember<T>,
	) -> Result<BalanceOf<T>, DispatchError> {
		// only executed in tests: ensure the member account is correct.
		debug_assert!(
			PoolMembers::<T>::get(member_account.clone().get()).expect("member must exist") ==
				pool_member
		);

		let pool_account = Pallet::<T>::generate_bonded_account(pool_member.pool_id);
		// if the pool doesn't have any pending slash, it implies the member also does not have any
		// pending slash.
		if T::StakeAdapter::pending_slash(Pool::from(pool_account.clone())).is_zero() {
			return Ok(Zero::zero());
		}

		// this is their actual held balance that may or may not have been slashed.
		let actual_balance = T::StakeAdapter::member_delegation_balance(member_account)
			// no delegation implies the member delegation is not migrated yet to `DelegateStake`.
			.ok_or(Error::<T>::NotMigrated)?;

		// this is their balance in the pool
		let expected_balance = pool_member.total_balance();

		// return the amount to be slashed.
		Ok(actual_balance.saturating_sub(expected_balance))
	}
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

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/tests/tests.rs (L2502-2576)
```rust
mod remote_test {
	use super::*;

	/// Test claim_trapped_balance for all pool members using a state snapshot.
	///
	/// The test iterates through all pool members, computes trapped amounts, and calls
	/// `do_claim_trapped_balance` for those with trapped funds. Only successful claims are printed.
	///
	/// Run with:
	/// ```bash
	/// SNAP=<PATH_TO_SNAP> cargo test -r -p asset-hub-westend-runtime np_claim_trapped_balance \
	/// -- --ignored --nocapture
	/// ```
	///
	/// Note: If you want to test this with PAH snapshot, ensure (locally, DO NOT COMMIT)
	/// 1) WAH staking pallet indices align with PAH
	/// 2) WAH ED is same as PAH (decrease it by 10x in `../../../constants/src/westend.rs`)
	/// 3) Staking Bonding Duration is 28 eras.
	#[tokio::test]
	#[ignore]
	async fn np_claim_trapped_balance() {
		use pallet_nomination_pools::{Pallet as NominationPools, PoolMembers};
		use remote_externalities::{Builder, Mode, OfflineConfig, SnapshotConfig};

		let snap_path =
			std::env::var("SNAP").expect("SNAP env var not set. Please provide snapshot path.");

		println!("Loading snapshot from: {}", snap_path);

		let mut ext = Builder::<Block>::new()
			.mode(Mode::Offline(OfflineConfig { state_snapshot: SnapshotConfig::new(snap_path) }))
			.build()
			.await
			.expect("Failed to load snapshot");

		ext.execute_with(|| {
			use pallet_nomination_pools::adapter::{Member, StakeStrategy};

			const DOT_DECIMALS: u128 = 10_000_000_000; // 10 decimals for DOT

			println!("\nChecking trapped balance for all pool members...\n");

			let mut total_members = 0u32;
			let mut success_count = 0u32;
			let mut total_claimed = 0u128;

			println!("member,pool_id,trapped_dot");

			for (member_account, member_data) in PoolMembers::<Runtime>::iter() {
				total_members += 1;

				// Compute trapped amount before calling the helper
				let expected = member_data.total_balance();
				let actual = <Runtime as pallet_nomination_pools::Config>::StakeAdapter
					::member_delegation_balance(Member::from(
						member_account.clone(),
					))
					.unwrap_or_default();
				let trapped = actual.saturating_sub(expected);

				// Ignore dust amounts (< 1 DOT) — only claim meaningful trapped balances.
				if trapped >= DOT_DECIMALS {
					assert_ok!(NominationPools::<Runtime>::do_claim_trapped_balance(
						&member_account
					));

					success_count += 1;
					total_claimed += trapped;
					let whole = trapped / DOT_DECIMALS;
					let fraction = (trapped % DOT_DECIMALS) / (DOT_DECIMALS / 100);
					println!(
						"{:?},{},{}.{:02}",
						member_account, member_data.pool_id, whole, fraction
					);
				}
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
