Based on the evidence gathered, here is the analog finding.

### Title
Restricted (disapproved) stash accounts can keep validating/nominating and earning staking rewards indefinitely because `Filter` is enforced only on `bond`/`bond_extra`/`rebond`, and `chill_other` cannot force them out - (File: `substrate/frame/staking/src/pallet/mod.rs`, `substrate/frame/staking-async/src/pallet/mod.rs`)

### Summary
The `pallet-staking` (and `pallet-staking-async`) `T::Filter: Contains<AccountId>` mechanism was introduced to "restrict certain accounts from staking" (e.g., accounts already staking indirectly via nomination pools, to prevent double-counting of stake/rewards) [1](#0-0) . This is the direct structural analog of MToken's "approved earners list": an account can be moved from "allowed" to "disapproved" (restricted) state. Just like the mZero bug, once an account is already an active validator/nominator, being placed on the restrict list does not stop it from continuing to earn — only the account's own voluntary `chill` removes it, and the permissionless `chill_other` extrinsic does not check the `Filter`/restricted state at all.

### Finding Description
`bond`, `bond_extra`, and `rebond` explicitly check `ensure!(!T::Filter::contains(&stash), Error::<T>::Restricted)` before allowing new staking actions [2](#0-1) [3](#0-2) . However, an account that is already bonded, validating, or nominating before being added to the restrict list is never automatically un-staked, un-validated, or un-nominated: the check only gates new bond/extra-bond/rebond operations, not the continued accrual of era rewards for an existing active nominator/validator role.

The only way to remove an active role is `chill` (self-only, permissionless call for the account itself) or `chill_other`, which is meant to be the "anyone can force removal" mechanism analogous to the report's recommended fix. But `chill_other`'s permissionless path is gated by unrelated conditions — either the nominator's entry is non-decodable, or a `ChillThreshold` + `MaxNominatorCount`/`MaxValidatorCount` + minimum bond check is satisfied [4](#0-3) . Nowhere does `chill_other` consult `T::Filter::contains(&stash)`. This is the exact same broken invariant as the M-1 report: "only the disapproved entity themselves can choose to stop earning; no one else has the authority to force them to quit."

The provided test `restricted_accounts_can_only_withdraw` demonstrates the gap directly: once `alice` is restricted, she is blocked from `bond_extra`/`rebond`, but she can still `unbond`/`withdraw_unbonded` her own funds — the test never asserts that an already-active validator/nominator role of a newly-restricted account is force-chilled, and no such mechanism exists in the pallet [5](#0-4) . Combined with `chill_other`'s unrelated gating conditions [6](#0-5) , a restricted stash that was already validating/nominating before restriction keeps earning until it self-chills.

### Impact Explanation
The `Filter`/`Restricted` mechanism exists specifically to prevent accounts from double-dipping into staking rewards (e.g., simultaneously being a direct nominator and a nomination-pool member) [1](#0-0) . If an account becomes restricted only after already being an active nominator/validator, it continues to receive era-validator/nominator rewards it was meant to be barred from, exactly like Alice in the mZero PoC continuing to accrue earner-rate rewards after removal from the approved list. This misallocates staking reward payouts (wrong set of eligible beneficiaries continuing to receive rewards), undermining the intended safety invariant the `Filter` was built to enforce.

### Likelihood Explanation
No privileged or malicious actor is required — a runtime operator (or governance) adding an account to the `Filter`/restrict list after that account is already staking is an entirely ordinary lifecycle event (this is explicitly the scenario the restrict-list feature was built for: an account starts staking directly, later joins a pool and is expected to be restricted from direct staking going forward). Anyone can attempt `chill_other`, but it will fail with `CannotChillOther` unless unrelated bond-threshold conditions happen to be met, meaning in the common/default configuration (no `ChillThreshold`/count limits set) there is no way for anyone but the restricted account itself to stop it from continuing to earn.

### Recommendation
Extend `chill_other` (or add a dedicated permissionless call) to also succeed when `T::Filter::contains(&stash)` is true, mirroring the report's suggested fix of allowing "anyone to stop the disapproved earner from earning":
```rust
if T::Filter::contains(&stash) {
    Self::chill_stash(&stash);
    return Ok(());
}
```
placed alongside the existing non-decodable-nominator early-return in `chill_other`.

### Proof of Concept
1. Runtime configures `T::Filter` to restrict pool members from direct staking (as in `existing_pool_member_cannot_stake`/`stakers_cannot_join_pool` tests) [7](#0-6) .
2. Account `A` bonds and calls `validate`/`nominate` while *not* restricted, becoming an active validator/nominator.
3. Governance/operator adds `A` to the restrict list (e.g., because `A` also joined a pool, or policy changed).
4. `A` remains in `Validators`/`Nominators` storage and keeps receiving era payouts every era, since `Filter` is never checked by the reward-payout path or by `chill_other`.
5. Any third party calling `Staking::chill_other(origin, A)` fails with `Error::CannotChillOther` unless the unrelated `ChillThreshold`/`MinNominatorBond` conditions happen to be satisfied [8](#0-7) .
6. Only `A` calling `chill` itself removes it from earning further rewards — reproducing the "only the disapproved entity can choose to stop earning" invariant break from the source report.

### Citations

**File:** prdoc/stable2503/pr_7685.prdoc (L1-8)
```text
title: 'Introduce filters to restrict accounts from staking'

doc:
  - audience: Runtime Dev
    description: |
      Introduce filters to restrict accounts from staking.
      This is useful for restricting certain accounts from staking, for example, accounts staking via pools, and vice
      versa.
```

**File:** substrate/frame/staking/src/pallet/mod.rs (L1187-1189)
```rust
			let stash = ensure_signed(origin)?;

			ensure!(!T::Filter::contains(&stash), Error::<T>::Restricted);
```

**File:** substrate/frame/staking/src/pallet/mod.rs (L1928-1956)
```rust
		/// Declare a `controller` to stop participating as either a validator or nominator.
		///
		/// Effects will be felt at the beginning of the next era.
		///
		/// The dispatch origin for this call must be _Signed_, but can be called by anyone.
		///
		/// If the caller is the same as the controller being targeted, then no further checks are
		/// enforced, and this function behaves just like `chill`.
		///
		/// If the caller is different than the controller being targeted, the following conditions
		/// must be met:
		///
		/// * `controller` must belong to a nominator who has become non-decodable,
		///
		/// Or:
		///
		/// * A `ChillThreshold` must be set and checked which defines how close to the max
		///   nominators or validators we must reach before users can start chilling one-another.
		/// * A `MaxNominatorCount` and `MaxValidatorCount` must be set which is used to determine
		///   how close we are to the threshold.
		/// * A `MinNominatorBond` and `MinValidatorBond` must be set and checked, which determines
		///   if this is a person that should be chilled because they have not met the threshold
		///   bond required.
		///
		/// This can be helpful if bond requirements are updated, and we need to remove old users
		/// who do not satisfy these requirements.
		#[pallet::call_index(23)]
		#[pallet::weight(T::WeightInfo::chill_other())]
		pub fn chill_other(origin: OriginFor<T>, stash: T::AccountId) -> DispatchResult {
```

**File:** substrate/frame/staking/src/pallet/mod.rs (L1983-2013)
```rust
			if Nominators::<T>::contains_key(&stash) && Nominators::<T>::get(&stash).is_none() {
				Self::chill_stash(&stash);
				return Ok(());
			}

			if caller != controller {
				let threshold = ChillThreshold::<T>::get().ok_or(Error::<T>::CannotChillOther)?;
				let min_active_bond = if Nominators::<T>::contains_key(&stash) {
					let max_nominator_count =
						MaxNominatorsCount::<T>::get().ok_or(Error::<T>::CannotChillOther)?;
					let current_nominator_count = Nominators::<T>::count();
					ensure!(
						threshold * max_nominator_count < current_nominator_count,
						Error::<T>::CannotChillOther
					);
					MinNominatorBond::<T>::get()
				} else if Validators::<T>::contains_key(&stash) {
					let max_validator_count =
						MaxValidatorsCount::<T>::get().ok_or(Error::<T>::CannotChillOther)?;
					let current_validator_count = Validators::<T>::count();
					ensure!(
						threshold * max_validator_count < current_validator_count,
						Error::<T>::CannotChillOther
					);
					MinValidatorBond::<T>::get()
				} else {
					Zero::zero()
				};

				ensure!(ledger.active < min_active_bond, Error::<T>::CannotChillOther);
			}
```

**File:** substrate/frame/staking-async/src/pallet/mod.rs (L1857-1859)
```rust
			let stash = ensure_signed(origin)?;

			ensure!(!T::Filter::contains(&stash), Error::<T>::Restricted);
```

**File:** substrate/frame/staking/src/tests.rs (L4668-4696)
```rust
#[test]
fn restricted_accounts_can_only_withdraw() {
	ExtBuilder::default().build_and_execute(|| {
		start_active_era(1);
		// alice is a non blacklisted account.
		let alice = 301;
		let _ = Balances::make_free_balance_be(&alice, 500);
		// alice can bond
		assert_ok!(Staking::bond(RuntimeOrigin::signed(alice), 100, RewardDestination::Staked));
		// and bob is a blacklisted account
		let bob = 302;
		let _ = Balances::make_free_balance_be(&bob, 500);
		restrict(&bob);

		// Bob cannot bond
		assert_noop!(
			Staking::bond(RuntimeOrigin::signed(bob), 100, RewardDestination::Staked,),
			Error::<Test>::Restricted
		);

		// alice is blacklisted now and cannot bond anymore
		restrict(&alice);
		assert_noop!(
			Staking::bond_extra(RuntimeOrigin::signed(alice), 100),
			Error::<Test>::Restricted
		);
		// but she can unbond her existing bond
		assert_ok!(Staking::unbond(RuntimeOrigin::signed(alice), 100));

```

**File:** substrate/frame/staking/src/tests.rs (L5358-5365)
```rust
			assert_noop!(
				Staking::chill_other(RuntimeOrigin::signed(1337), 0),
				Error::<Test>::CannotChillOther
			);
			assert_noop!(
				Staking::chill_other(RuntimeOrigin::signed(1337), 2),
				Error::<Test>::CannotChillOther
			);
```

**File:** substrate/frame/delegated-staking/src/tests.rs (L1327-1379)
```rust
	#[test]
	fn existing_pool_member_cannot_stake() {
		// A pool member is able to stake directly since staking only uses free funds but once a
		// staker, they cannot join/add extra bond to the pool. They can still withdraw funds.
		ExtBuilder::default().build_and_execute(|| {
			start_era(1);
			// GIVEN: a pool.
			fund(&200, 1000);
			let pool_id = create_pool(200, 800);

			// WHEN: delegator joins a pool
			let delegator = 100;
			fund(&delegator, 1000);
			assert_ok!(Pools::join(RawOrigin::Signed(delegator).into(), 200, pool_id));

			// THEN: they cannot stake anymore
			assert_noop!(
				Staking::bond(
					RuntimeOrigin::signed(delegator),
					500,
					RewardDestination::Account(101)
				),
				StakingError::<T>::Restricted
			);
		});
	}

	#[test]
	fn stakers_cannot_join_pool() {
		ExtBuilder::default().build_and_execute(|| {
			start_era(1);
			// GIVEN: a pool.
			fund(&200, 1000);
			let pool_id = create_pool(200, 800);

			// WHEN: an account is a staker.
			let staker = 100;
			fund(&staker, 1000);

			assert_ok!(Staking::bond(
				RuntimeOrigin::signed(staker),
				500,
				RewardDestination::Account(101)
			));
			assert_ok!(Staking::nominate(RuntimeOrigin::signed(staker), vec![GENESIS_VALIDATOR]));

			// THEN: they cannot join pool.
			assert_noop!(
				Pools::join(RawOrigin::Signed(staker).into(), 200, pool_id),
				PoolsError::<T>::Restricted
			);
		});
	}
```
