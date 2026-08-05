### Title
Nomination pool commission payout pays the *current* commission payee for pending commission accrued under a *previous* payee/root, not the payee entitled at accrual time - (File: `substrate/frame/nomination-pools/src/lib.rs`)

### Summary
`pallet-nomination-pools` accumulates unclaimed commission in a single undifferentiated pot, `RewardPool::total_commission_pending`, and pays the whole pot to whichever account is recorded as the *current* commission payee at the moment `claim_commission` is executed [1](#0-0) . Because the commission payee (and the `root` role that controls it) can be reassigned at any time, and because commission accrual is tracked as a single running balance rather than being attributed to the payee that was active while it accrued, a payee/root change after commission has accrued but before it is claimed causes the new payee to receive commission that was earned while a different account held that entitlement. This is the same broken invariant as the reported "Rewards" contract bug: reward state is evaluated against the *current* owner instead of the owner *during the period the reward was earned*.

### Finding Description
`Commission<T>` stores the payee as `current: Option<(Perbill, T::AccountId)>` [2](#0-1) . The pool `root` role is explicitly documented as able to "manage and claim commission" and to "change ... itself" (i.e. transfer the root role, and by extension control of the commission payee) [3](#0-2) .

When commission is claimed, `do_claim_commission`:
1. Calls `reward_pool.update_records(...)` to roll any newly-accrued commission (computed from reward-counter deltas at the *current* commission rate) into `total_commission_pending` [4](#0-3) .
2. Reads the payee purely from `bonded_pool.commission.current` — i.e., whoever is configured *right now* [5](#0-4) .
3. Transfers the *entire* `total_commission_pending` balance to that payee, then zeroes the pending counter [6](#0-5) .

`total_commission_pending` is a single scalar with no per-payee/per-period attribution. The codebase already recognizes and partially mitigates one dimension of this problem — a rate change: `set_commission_max_snapshots_rewards_before_lowering_current` demonstrates that lowering `commission.max` force-snapshots pending commission at the pre-cut rate before applying a lower current rate, specifically to prevent commission earned at a higher rate from being "leaked to members" at claim time [7](#0-6) . However, there is no equivalent snapshot for the **payee identity**: `total_commission_pending` still just accumulates the correct amount, but when it is finally claimed, 100% of it (including any portion whose entitled payee/root has since changed) is paid to whoever is `commission.current`'s payee at that moment.

If pool `root` is transferred (or the payee is updated via `set_commission`) after commission has accrued but before `claim_commission` is called, the new root/payee — who did not curate/manage the pool during the accrual window — receives the previous root's earned commission in full, exactly mirroring the reported vault-owner bug (`VaultTokenized(vault).owner()` fetched at distribution time instead of at the accrual period).

### Impact Explanation
An outgoing pool `root`/commission-payee permanently loses commission that accrued for their period of service, and an incoming root/payee who neither managed the pool nor bore its risk during that window receives it instead. This misroutes real, on-chain value (the pool's reward-account balance) to the wrong beneficiary and violates the "settle exactly once to the rightful beneficiary and amount" invariant for reward payouts.

### Likelihood Explanation
No governance, validator, relayer, or malicious-peer assumption is required: `set_commission` and `update_roles`/payee changes are normal, permissionless-to-the-role operations that any pool root is expected to perform routinely (e.g., selling/handing off a pool, rotating a treasury address, or simply updating the payout account). Any pool where commission accrues between two consecutive `claim_commission` calls and where the root/payee is changed in that interval will trigger the misattribution — this is a realistic, easily triggered sequence rather than an edge case.

### Recommendation
Before allowing a change to `commission.current`'s payee (in `set_commission`) or a transfer of the `root` role (`update_roles`), force a settlement/snapshot: call `update_records` to roll all currently-accrued commission into `total_commission_pending` and immediately pay out (or checkpoint the amount against) the *outgoing* payee before the payee/root can be swapped, rather than leaving it in a shared pot that a new payee can later drain in full.

### Proof of Concept
1. Root account `A` sets pool commission to a non-zero rate with payee `A`: `Pools::set_commission(root=A, pool_id, Some((rate, A)))`.
2. The pool accrues rewards (e.g. via `deposit_rewards`/bonding activity) over some period, generating pending commission that, per `update_records`, becomes owed to whoever is recorded as payee.
3. Root `A` transfers root/payee control (e.g., `Pools::set_commission(root=A, pool_id, Some((rate, B)))` or a root-role transfer) to account `B`, without calling `claim_commission` first.
4. `B` calls `Pools::claim_commission(RuntimeOrigin::signed(B), pool_id)`.
5. Per `do_claim_commission`, the full `total_commission_pending` (which includes commission accrued while `A` was payee) is transferred to `B` [6](#0-5) ; `A` has no remaining claim and receives nothing for the period it was the recorded payee, reproducing the exact "new owner takes past-period reward" scenario from the external report.

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L131-134)
```rust
//! * Nominator: can select which validators the pool nominates.
//! * Bouncer: can change the pools state and kick members if the pool is blocked.
//! * Root: can change the nominator, bouncer, or itself, manage and claim commission, and can
//!   perform any of the actions the nominator or bouncer can.
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L778-781)
```rust
pub struct Commission<T: Config> {
	/// Optional commission rate of the pool along with the account commission is paid to.
	pub current: Option<(Perbill, T::AccountId)>,
	/// Optional maximum commission that can be set by the pool `root`. Once set, this value can
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L3709-3746)
```rust
	fn do_claim_commission(who: T::AccountId, pool_id: PoolId) -> DispatchResult {
		let bonded_pool = BondedPool::<T>::get(pool_id).ok_or(Error::<T>::PoolNotFound)?;
		ensure!(bonded_pool.can_claim_commission(&who), Error::<T>::DoesNotHavePermission);

		let mut reward_pool = RewardPools::<T>::get(pool_id)
			.defensive_ok_or::<Error<T>>(DefensiveError::RewardPoolNotFound.into())?;

		// IMPORTANT: ensure newly pending commission not yet processed is added to
		// `total_commission_pending`.
		reward_pool.update_records(
			pool_id,
			bonded_pool.points,
			bonded_pool.commission.current(),
		)?;

		let commission = reward_pool.total_commission_pending;
		ensure!(!commission.is_zero(), Error::<T>::NoPendingCommission);

		let payee = bonded_pool
			.commission
			.current
			.as_ref()
			.map(|(_, p)| p.clone())
			.ok_or(Error::<T>::NoCommissionCurrentSet)?;

		// Payout claimed commission.
		T::Currency::transfer(
			&bonded_pool.reward_account(),
			&payee,
			commission,
			Preservation::Preserve,
		)?;

		// Add pending commission to total claimed counter.
		reward_pool.total_commission_claimed =
			reward_pool.total_commission_claimed.saturating_add(commission);
		// Reset total pending commission counter to zero.
		reward_pool.total_commission_pending = Zero::zero();
```

**File:** substrate/frame/nomination-pools/src/tests.rs (L6990-7020)
```rust
	#[test]
	fn set_commission_max_snapshots_rewards_before_lowering_current() {
		// `set_commission_max` force-lowers `current` when the new max is below it. Rewards that
		// accrued at the higher rate since the last snapshot must stay owed to the payee at that
		// higher rate, not be re-rated at the new lower rate and leaked to members.
		ExtBuilder::default().build_and_execute(|| {
			let pool_id = 1;
			let payee = 900;
			let _ = Currency::set_balance(&payee, 5);

			// GIVEN: commission is 50% (this snapshots the still-empty reward pool)...
			assert_ok!(Pools::set_commission(
				RuntimeOrigin::signed(900),
				pool_id,
				Some((Perbill::from_percent(50), payee))
			));
			// ...and 100 of rewards accrue with no intervening snapshot (no claim/bond happens).
			deposit_rewards(100);
			assert_eq!(RewardPool::<Runtime>::current_balance(pool_id), 100);
			assert_eq!(RewardPools::<Runtime>::get(pool_id).unwrap().total_commission_pending, 0);

			// WHEN: root force-lowers max commission to 20%, cutting `current` from 50% to 20%.
			assert_ok!(Pools::set_commission_max(
				RuntimeOrigin::signed(900),
				pool_id,
				Perbill::from_percent(20)
			));

			// THEN: the 100 that accrued at 50% was snapshotted before the cut, so 50 is owed to
			// the payee. Without the pre-cut snapshot this would be 20% * 100 = 20.
			assert_eq!(RewardPools::<Runtime>::get(pool_id).unwrap().total_commission_pending, 50);
```
