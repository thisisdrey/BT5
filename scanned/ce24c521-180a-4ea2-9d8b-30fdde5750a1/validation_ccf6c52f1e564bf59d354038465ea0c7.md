### Title
JIT reward sniping in `pallet-nomination-pools`: instant `join` before a reward deposit lets an attacker claim yield they never earned - (File: `substrate/frame/nomination-pools/src/lib.rs`)

### Summary
`pallet-nomination-pools` distributes the reward pool's balance to members strictly proportional to their **current** points at the time of `update_records`, with no time-weighting or lock-up tying a member's share to how long their bond actually contributed to earning that reward. This mirrors the `LiquidityReserve` bug: shares are minted/valued instantly and fees/yield are distributed instantly per current share, enabling a JIT-style attack where a party deposits capital immediately before a payout lands, captures a disproportionate cut, then withdraws.

### Finding Description
When a member calls `join`, the pool first calls `reward_pool.update_records(pool_id, bonded_pool.points, ...)` using the **old** point total, correctly preventing the new joiner from stealing rewards that are *already sitting* in the reward account. The joiner's `last_recorded_reward_counter` is set to the resulting counter, so they start with zero claim on the past-accrued balance. [1](#0-0) 

However, once joined, `bonded_pool.points` is bumped immediately via `try_bond_funds`, so the new member's points participate fully in the pro-rata split of **any reward that arrives afterward** — including a payout that the attacker anticipated and that corresponds to staking rewards earned by the pool's *existing* stake during an era in which the new joiner had no exposure at all. `RewardPool::current_reward_counter`/`update_records` compute the payable balance purely from `current_balance(id)` and `bonded_points` (i.e., points *now*), with no dependency on how long each point has been held: [2](#0-1) [3](#0-2) 

Staking rewards for a nomination pool's bonded stash are paid out via the public, permissionless `payout_stakers` extrinsic in `pallet-staking`/`pallet-staking-async`, which transfers a validator's era reward to the pool's reward account based on the **exposure snapshot taken at the start of that historical era** — i.e., independent of the pool's current bonded amount. This creates a timing gap: exposure/reward amount is fixed by past stake, but the *split of that reward among pool members* is fixed by points *at the moment the reward lands*, not at the moment the exposure was earned.

An attacker can therefore:
1. Watch for an imminent (or manually triggerable via `payout_stakers`) era-reward deposit into a pool's reward account.
2. Call `join` (or `bond_extra`) with a large amount right before the payout lands, instantly inflating their share of `bonded_pool.points` relative to everyone else's.
3. Call `claim_payout` (`Call::claim_payout`, doc'd to pay “pro rata based on the members stake vs the sum of the members in the pools stake”) immediately after the reward balance increases, extracting a share of yield proportional to their newly-minted points even though their capital contributed nothing to earning it. [4](#0-3) 
4. Call `unbond` to exit — `unbond` explicitly “implicitly collects the rewards one last time” and has no minimum holding period gating reward eligibility, only the bonding-duration delay for *principal* withdrawal, not for the already-realized claimed payout. [5](#0-4) 

This is the direct analog of the `LiquidityReserve` H-04 finding: value is shared per **current** share of a pool at the instant a fee/reward materializes, with no mechanism to time-weight contributions or delay/lock share eligibility, so anyone can add capital right before the payout and remove it right after, diluting honest long-term stakers.

### Impact Explanation
Long-term nomination-pool members have their earned staking rewards diluted by opportunistic joiners who contribute capital for a matter of blocks. Because pool sizes can be large and joins/`bond_extra` are unrestricted (subject only to `MinJoinBond` and `MaxPoolMembersPerPool`), an attacker with sufficient capital (potentially flash-borrowed elsewhere, deposited, then withdrawn) can extract a meaningful fraction of a payout that legitimate members earned through actual bonded exposure over the era. This is a real value-transfer from honest stakers to the attacker, matching the "theft of yield belonging to other participants" impact class.

### Likelihood Explanation
`payout_stakers` is a public, permissionless extrinsic that any account can call once an era's reward is due, giving an attacker precise, controllable timing over when the reward lands in the pool's reward account — unlike a DEX swap where JIT profitability depends on unpredictable trade flow. Reward amounts for large/popular pools can be substantial, and `join`/`bond_extra`/`claim_payout`/`unbond` are all ordinary unprivileged calls, requiring no validator, governance, or admin cooperation. This makes the attack straightforward to execute deterministically whenever an attacker has enough capital.

### Recommendation
- Time-weight reward eligibility: track how long a member's points have been active (or snapshot points at era boundaries) before allowing them to participate in a given reward distribution, rather than using only the instantaneous point total at `update_records` time.
- Alternatively, delay newly bonded points from counting toward reward-counter distributions until a cool-down (e.g., one full era) has elapsed, similar to how `unbond` already delays principal withdrawal.
- Consider streaming/vesting large reward deposits into the reward pool's claimable balance over multiple blocks/eras instead of making them instantly claimable in full.

### Proof of Concept
1. Pool P has members with 100 total points, having been bonded for a full era during which a validator earned reward R (paid out via `payout_stakers` from `pallet-staking`).
2. Just before `payout_stakers` is called for that era, attacker calls `Pools::join(origin, huge_amount, pool_id)`, causing `bonded_pool.points` to jump to, say, 1000 (attacker holds 900 of them). `update_records` correctly excludes attacker from any *already-recorded* past payouts, but the new points are now part of the pool going forward. [6](#0-5) 
3. Anyone (attacker or a bystander) calls `pallet_staking::payout_stakers`, transferring R into the pool's reward account.
4. Attacker calls `Pools::claim_payout(origin)`; `do_reward_payout` computes their share via `current_reward_counter`, giving them ~90% of R despite zero era-time exposure, as demonstrated by the pro-rata mechanics validated in `rewards_distribution_is_fair_basic`/`rewards_distribution_is_fair_3` tests where any pending reward is split purely by current points at claim time. [7](#0-6) 
5. Attacker calls `Pools::unbond(origin, attacker, huge_amount)` to exit, having already realized the stolen yield in step 4, leaving the original long-term members with a diluted share of R.

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L1448-1470)
```rust
	/// Get the current reward counter, based on the given `bonded_points` being the state of the
	/// bonded pool at this time.
	fn current_reward_counter(
		&self,
		id: PoolId,
		bonded_points: BalanceOf<T>,
		commission: Perbill,
	) -> Result<(T::RewardCounter, BalanceOf<T>), Error<T>> {
		let balance = Self::current_balance(id);

		// Calculate the current payout balance. The first 3 values of this calculation added
		// together represent what the balance would be if no payouts were made. The
		// `last_recorded_total_payouts` is then subtracted from this value to cancel out previously
		// recorded payouts, leaving only the remaining payouts that have not been claimed.
		let current_payout_balance = balance
			.saturating_add(self.total_rewards_claimed)
			.saturating_add(self.total_commission_claimed)
			.saturating_sub(self.last_recorded_total_payouts);

		// Split the `current_payout_balance` into claimable rewards and claimable commission
		// according to the current commission rate.
		let new_pending_commission = commission * current_payout_balance;
		let new_pending_rewards = current_payout_balance.saturating_sub(new_pending_commission);
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L1506-1511)
```rust
		let current_reward_counter =
			T::RewardCounter::checked_from_rational(new_pending_rewards, bonded_points)
				.and_then(|ref r| self.last_recorded_reward_counter.checked_add(r))
				.ok_or(Error::<T>::OverflowRisk)?;

		Ok((current_reward_counter, new_pending_commission))
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L2118-2161)
```rust
		#[pallet::call_index(0)]
		#[pallet::weight(T::WeightInfo::join())]
		pub fn join(
			origin: OriginFor<T>,
			#[pallet::compact] amount: BalanceOf<T>,
			pool_id: PoolId,
		) -> DispatchResult {
			let who = ensure_signed(origin)?;
			// ensure pool is not in an un-migrated state.
			ensure!(!Self::api_pool_needs_delegate_migration(pool_id), Error::<T>::NotMigrated);

			// ensure account is not restricted from joining the pool.
			ensure!(!T::Filter::contains(&who), Error::<T>::Restricted);

			ensure!(amount >= MinJoinBond::<T>::get(), Error::<T>::MinimumBondNotMet);
			// If a member already exists that means they already belong to a pool
			ensure!(!PoolMembers::<T>::contains_key(&who), Error::<T>::AccountBelongsToOtherPool);

			let mut bonded_pool = BondedPool::<T>::get(pool_id).ok_or(Error::<T>::PoolNotFound)?;
			bonded_pool.ok_to_join()?;

			let mut reward_pool = RewardPools::<T>::get(pool_id)
				.defensive_ok_or::<Error<T>>(DefensiveError::RewardPoolNotFound.into())?;
			// IMPORTANT: reward pool records must be updated with the old points.
			reward_pool.update_records(
				pool_id,
				bonded_pool.points,
				bonded_pool.commission.current(),
			)?;

			bonded_pool.try_inc_members()?;
			let points_issued = bonded_pool.try_bond_funds(&who, amount, BondType::Extra)?;

			PoolMembers::insert(
				who.clone(),
				PoolMember::<T> {
					pool_id,
					points: points_issued,
					// we just updated `last_known_reward_counter` to the current one in
					// `update_recorded`.
					last_recorded_reward_counter: reward_pool.last_recorded_reward_counter(),
					unbonding_eras: Default::default(),
				},
			);
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L2203-2221)
```rust
		/// A bonded member can use this to claim their payout based on the rewards that the pool
		/// has accumulated since their last claimed payout (OR since joining if this is their first
		/// time claiming rewards). The payout will be transferred to the member's account.
		///
		/// The member will earn rewards pro rata based on the members stake vs the sum of the
		/// members in the pools stake. Rewards do not "expire".
		///
		/// See `claim_payout_other` to claim rewards on behalf of some `other` pool member.
		#[pallet::call_index(2)]
		#[pallet::weight(T::WeightInfo::claim_payout())]
		pub fn claim_payout(origin: OriginFor<T>) -> DispatchResult {
			let signer = ensure_signed(origin)?;
			// ensure signer is not in an un-migrated state.
			ensure!(
				!Self::api_member_needs_delegate_migration(signer.clone()),
				Error::<T>::NotMigrated
			);

			Self::do_claim_payout(signer.clone(), signer)
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L2224-2260)
```rust
		/// Unbond up to `unbonding_points` of the `member_account`'s funds from the pool. It
		/// implicitly collects the rewards one last time, since not doing so would mean some
		/// rewards would be forfeited.
		///
		/// Under certain conditions, this call can be dispatched permissionlessly (i.e. by any
		/// account).
		///
		/// # Conditions for a permissionless dispatch.
		///
		/// * The pool is blocked and the caller is either the root or bouncer. This is refereed to
		///   as a kick.
		/// * The pool is destroying and the member is not the depositor.
		/// * The pool is destroying, the member is the depositor and no other members are in the
		///   pool.
		///
		/// ## Conditions for permissioned dispatch (i.e. the caller is also the
		/// `member_account`):
		///
		/// * The caller is not the depositor.
		/// * The caller is the depositor, the pool is destroying and no other members are in the
		///   pool.
		///
		/// # Note
		///
		/// If there are too many unlocking chunks to unbond with the pool account,
		/// [`Call::pool_withdraw_unbonded`] can be called to try and minimize unlocking chunks.
		/// The [`StakingInterface::unbond`] will implicitly call [`Call::pool_withdraw_unbonded`]
		/// to try to free chunks if necessary (ie. if unbound was called and no unlocking chunks
		/// are available). However, it may not be possible to release the current unlocking chunks,
		/// in which case, the result of this call will likely be the `NoMoreChunks` error from the
		/// staking system.
		#[pallet::call_index(3)]
		#[pallet::weight(T::WeightInfo::unbond())]
		pub fn unbond(
			origin: OriginFor<T>,
			member_account: AccountIdLookupOf<T>,
			#[pallet::compact] unbonding_points: BalanceOf<T>,
```

**File:** substrate/frame/nomination-pools/src/tests.rs (L1782-1816)
```rust
	#[test]
	fn rewards_distribution_is_fair_3() {
		ExtBuilder::default().build_and_execute(|| {
			let ed = Currency::minimum_balance();

			deposit_rewards(30);

			Currency::set_balance(&20, ed + 10);
			assert_ok!(Pools::join(RuntimeOrigin::signed(20), 10, 1));

			deposit_rewards(100);

			Currency::set_balance(&30, ed + 10);
			assert_ok!(Pools::join(RuntimeOrigin::signed(30), 10, 1));

			deposit_rewards(60);

			// 10 should claim 10, 20 should claim nothing.
			assert_ok!(Pools::claim_payout(RuntimeOrigin::signed(10)));
			assert_ok!(Pools::claim_payout(RuntimeOrigin::signed(20)));
			assert_ok!(Pools::claim_payout(RuntimeOrigin::signed(30)));

			assert_eq!(
				pool_events_since_last_call(),
				vec![
					Event::Created { depositor: 10, pool_id: 1 },
					Event::Bonded { member: 10, pool_id: 1, bonded: 10, joined: true },
					Event::MetadataUpdated { pool_id: 1, caller: 900 },
					Event::Bonded { member: 20, pool_id: 1, bonded: 10, joined: true },
					Event::Bonded { member: 30, pool_id: 1, bonded: 10, joined: true },
					Event::PaidOut { member: 10, pool_id: 1, payout: 30 + 100 / 2 + 60 / 3 },
					Event::PaidOut { member: 20, pool_id: 1, payout: 100 / 2 + 60 / 3 },
					Event::PaidOut { member: 30, pool_id: 1, payout: 60 / 3 },
				]
			);
```
