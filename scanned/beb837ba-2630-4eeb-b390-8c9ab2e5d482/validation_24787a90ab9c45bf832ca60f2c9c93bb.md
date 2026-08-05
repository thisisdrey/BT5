## Analysis

The Furnace bug's core broken invariant: **share/exchange-rate updates happen in discrete, predictable jumps that are decoupled from the time a participant actually held risk, and any public entry-point that mutates the participant's share count around that jump lets an attacker with zero holding-period capture proportional revenue meant for long-term holders.**

The closest local analog is in `pallet-nomination-pools`, whose reward-sharing model uses the same "cumulative reward-per-point counter" pattern as an ERC-4626-style vault, but where the counter only advances at the exact block a *pool-level* payout arrives, and that payout is triggered by the fully permissionless `payout_stakers`/`payout_stakers_by_page` extrinsics in `pallet-staking`.

### Title
Nomination-pool reward counter allows zero-duration "flash-staking" of era rewards via `join`/`bond_extra` timed around permissionless `payout_stakers` - (File: `substrate/frame/nomination-pools/src/lib.rs`)

### Summary
`pallet-nomination-pools` tracks member shares of pooled staking rewards through `RewardPool::current_reward_counter`/`update_records`, which derives the pool's per-point reward rate purely from the *current* balance of the pool's reward account versus `bonded_points` at the moment `update_records` is called [1](#0-0) . Because the underlying validator reward for the era is transferred into that reward account by the fully permissionless `payout_stakers`/`payout_stakers_by_page` calls in `pallet-staking` [2](#0-1) , an attacker can `join`/`bond_extra` into the pool in the same block, immediately *before* the reward transfer lands, then trigger (or wait for) `payout_stakers`, and finally `claim_payout` to extract a proportional share of a reward that accrued entirely before the attacker had any stake at risk.

### Finding Description
In `join`, the pool first calls `reward_pool.update_records(pool_id, bonded_pool.points, ...)` using the **old** `bonded_pool.points` (i.e. before the new member's stake is added), which fixes the new member's `last_recorded_reward_counter` to the reward rate that existed *before* their deposit [3](#0-2) . The same pattern is used in `do_bond_extra` [4](#0-3) .

This correctly prevents a joiner from stealing rewards that are *already sitting* in the reward account. However, it does not protect against the reverse timing: if the reward has **not yet been transferred** into the reward account (i.e., `payout_stakers` for that era has not yet been called for this validator/pool), the attacker's `join`/`bond_extra` is processed against the old, lower balance, and their `last_recorded_reward_counter` is pinned to that lower value. Once `payout_stakers` (or `payout_stakers_by_page`) is subsequently called — by anyone, since it is a fully permissionless, signed-origin dispatchable [5](#0-4)  — the reward lands in the pool's reward account. The *next* `update_records` call (triggered by any member's `claim_payout`, `bond_extra`, or `unbond`) computes `current_reward_counter` from the new, larger balance divided by `bonded_points`, which now **includes** the attacker's freshly-added points [6](#0-5) . Because the attacker's recorded counter was the old (lower) value, `do_reward_payout`'s `member.pending_rewards(current_reward_counter)` grants them a proportional share of the entire newly-arrived reward [7](#0-6) , even though they held zero effective stake during the era that generated it.

### Impact Explanation
Each additional point added by the attacker immediately before the payout lands increases the denominator (`bonded_points`) used to convert the newly-arrived, fixed-size reward into a per-point rate, so pre-existing long-term members receive a smaller reward-per-point than they otherwise would have — the reward is effectively diverted, in part, to the attacker's zero-duration position. This is a direct value-conservation/settlement violation: staking rewards must accrue to and settle for stakers who bore the era's risk, not for a depositor who joined mid-flight to time the payout. The attacker can then `claim_payout` immediately and later withdraw principal (the bonding delay only locks principal withdrawal, not reward claiming), realizing risk-free yield while diluting other pool members — matching the "wrong beneficiary or amount" impact category and the Furnace A2 pattern (get outsized yield for near-zero holding period).

### Likelihood Explanation
`payout_stakers`/`payout_stakers_by_page` are permissionless and their timing is largely predictable (once per era, any time within `HistoryDepth` eras) [8](#0-7) . No validator, collator, relayer, or governance actor is required — a single unprivileged account can self-trigger both the `bond_extra`/`join` and the subsequent `payout_stakers` call in the same or immediately following block, then `claim_payout`. This only requires ordinary transaction submission by an unprivileged user.

### Recommendation
Decouple reward eligibility from raw current points at the instant a payout lands, e.g. by requiring a minimum bonding duration (or era-boundary snapshot of points) before newly bonded points become eligible for a reward increment that was earned prior to the bond, or by having the nomination-pools adapter itself call `update_records` synchronously as part of the same atomic operation that receives the staking payout, before any pending `join`/`bond_extra` in the same block can be included ahead of it. Alternatively, snapshot per-member eligible points at era boundaries (similar to how `pallet-staking` itself uses era-end exposure) rather than using the live `bonded_pool.points` at the moment of `update_records`.

### Proof of Concept
1. Pool P has one long-term member M with all pool points; validator V (nominated by P) is due an era-`e` reward that has been calculated in `ErasValidatorReward`/exposure but not yet paid out via `payout_stakers`.
2. Attacker A calls `Pools::join(P, amount)` (or `bond_extra`). `update_records` runs against P's *current* reward-account balance (still pre-payout), so A's `last_recorded_reward_counter` is pinned low; A's points are added to `bonded_pool.points`.
3. In the same or next block, anyone (including A) calls `Staking::payout_stakers(V, e)` (or `payout_stakers_by_page`), which is permissionless per [9](#0-8) ; the era reward is transferred into P's reward account, now split over `bonded_pool.points` that include A's stake.
4. A calls `Pools::claim_payout()`. `do_reward_payout` computes `current_reward_counter` off the now-larger balance and A's larger point share, paying A a proportional slice of a reward earned entirely before A joined [10](#0-9) .
5. A calls `unbond`/`withdraw_unbonded` to exit, having captured yield with effectively zero at-risk holding time, at the expense of M's proportional share of the era's reward.

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L1408-1446)
```rust
	fn update_records(
		&mut self,
		id: PoolId,
		bonded_points: BalanceOf<T>,
		commission: Perbill,
	) -> Result<(), Error<T>> {
		let balance = Self::current_balance(id);

		let (current_reward_counter, new_pending_commission) =
			self.current_reward_counter(id, bonded_points, commission)?;

		// Store the reward counter at the time of this update. This is used in subsequent calls to
		// `current_reward_counter`, whereby newly pending rewards (in points) are added to this
		// value.
		self.last_recorded_reward_counter = current_reward_counter;

		// Add any new pending commission that has been calculated from `current_reward_counter` to
		// determine the total pending commission at the time of this update.
		self.total_commission_pending =
			self.total_commission_pending.saturating_add(new_pending_commission);

		// Total payouts are essentially the entire historical balance of the reward pool, equating
		// to the current balance + the total rewards that have left the pool + the total commission
		// that has left the pool.
		let last_recorded_total_payouts = balance
			.checked_add(&self.total_rewards_claimed.saturating_add(self.total_commission_claimed))
			.ok_or(Error::<T>::OverflowRisk)?;

		// Store the total payouts at the time of this update.
		//
		// An increase in ED could cause `last_recorded_total_payouts` to decrease but we should not
		// allow that to happen since an already paid out reward cannot decrease. The reward account
		// might go in deficit temporarily in this exceptional case but it will be corrected once
		// new rewards are added to the pool.
		self.last_recorded_total_payouts =
			self.last_recorded_total_payouts.max(last_recorded_total_payouts);

		Ok(())
	}
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L1448-1512)
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

		// * accuracy notes regarding the multiplication in `checked_from_rational`:
		// `current_payout_balance` is a subset of the total_issuance at the very worse.
		// `bonded_points` are similarly, in a non-slashed pool, have the same granularity as
		// balance, and are thus below within the range of total_issuance. In the worse case
		// scenario, for `saturating_from_rational`, we have:
		//
		// dot_total_issuance * 10^18 / `minJoinBond`
		//
		// assuming `MinJoinBond == ED`
		//
		// dot_total_issuance * 10^18 / 10^10 = dot_total_issuance * 10^8
		//
		// which, with the current numbers, is a miniscule fraction of the u128 capacity.
		//
		// Thus, adding two values of type reward counter should be safe for ages in a chain like
		// Polkadot. The important note here is that `reward_pool.last_recorded_reward_counter` only
		// ever accumulates, but its semantics imply that it is less than total_issuance, when
		// represented as `FixedU128`, which means it is less than `total_issuance * 10^18`.
		//
		// * accuracy notes regarding `checked_from_rational` collapsing to zero, meaning that no
		//   reward can be claimed:
		//
		// largest `bonded_points`, such that the reward counter is non-zero, with `FixedU128` will
		// be when the payout is being computed. This essentially means `payout/bonded_points` needs
		// to be more than 1/1^18. Thus, assuming that `bonded_points` will always be less than `10
		// * dot_total_issuance`, if the reward_counter is the smallest possible value, the value of
		//   the
		// reward being calculated is:
		//
		// x / 10^20 = 1/ 10^18
		//
		// x = 100
		//
		// which is basically 10^-8 DOTs. See `smallest_claimable_reward` for an example of this.
		let current_reward_counter =
			T::RewardCounter::checked_from_rational(new_pending_rewards, bonded_points)
				.and_then(|ref r| self.last_recorded_reward_counter.checked_add(r))
				.ok_or(Error::<T>::OverflowRisk)?;

		Ok((current_reward_counter, new_pending_commission))
	}
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L2139-2161)
```rust
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

**File:** substrate/frame/nomination-pools/src/lib.rs (L3539-3563)
```rust
		let (current_reward_counter, _) = reward_pool.current_reward_counter(
			bonded_pool.id,
			bonded_pool.points,
			bonded_pool.commission.current(),
		)?;

		// Determine the pending rewards. In scenarios where commission is 100%, `pending_rewards`
		// will be zero.
		let pending_rewards = member.pending_rewards(current_reward_counter)?;
		if pending_rewards.is_zero() {
			return Ok(pending_rewards);
		}

		// IFF the reward is non-zero alter the member and reward pool info.
		member.last_recorded_reward_counter = current_reward_counter;
		reward_pool.register_claimed_reward(pending_rewards);

		T::Currency::transfer(
			&bonded_pool.reward_account(),
			member_account,
			pending_rewards,
			// defensive: the depositor has put existential deposit into the pool and it stays
			// untouched, reward account shall not die.
			Preservation::Preserve,
		)?;
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L3668-3696)
```rust
		let (mut member, mut bonded_pool, mut reward_pool) =
			Self::get_member_with_pools(&member_account)?;

		// payout related stuff: we must claim the payouts, and updated recorded payout data
		// before updating the bonded pool points, similar to that of `join` transaction.
		reward_pool.update_records(
			bonded_pool.id,
			bonded_pool.points,
			bonded_pool.commission.current(),
		)?;
		let claimed = Self::do_reward_payout(
			&member_account,
			&mut member,
			&mut bonded_pool,
			&mut reward_pool,
		)?;

		let (points_issued, bonded) = match extra {
			BondExtra::FreeBalance(amount) => {
				(bonded_pool.try_bond_funds(&member_account, amount, BondType::Extra)?, amount)
			},
			BondExtra::Rewards => {
				(bonded_pool.try_bond_funds(&member_account, claimed, BondType::Extra)?, claimed)
			},
		};

		bonded_pool.ok_to_be_open()?;
		member.points =
			member.points.checked_add(&points_issued).ok_or(Error::<T>::OverflowRisk)?;
```

**File:** substrate/frame/staking/src/pallet/mod.rs (L1716-1738)
```rust
		/// Pay out next page of the stakers behind a validator for the given era.
		///
		/// - `validator_stash` is the stash account of the validator.
		/// - `era` may be any era between `[current_era - history_depth; current_era]`.
		///
		/// The origin of this call must be _Signed_. Any account can call this function, even if
		/// it is not one of the stakers.
		///
		/// The reward payout could be paged in case there are too many nominators backing the
		/// `validator_stash`. This call will payout unpaid pages in an ascending order. To claim a
		/// specific page, use `payout_stakers_by_page`.`
		///
		/// If all pages are claimed, it returns an error `InvalidPage`.
		#[pallet::call_index(18)]
		#[pallet::weight(T::WeightInfo::payout_stakers_alive_staked(T::MaxExposurePageSize::get()))]
		pub fn payout_stakers(
			origin: OriginFor<T>,
			validator_stash: T::AccountId,
			era: EraIndex,
		) -> DispatchResultWithPostInfo {
			ensure_signed(origin)?;
			Self::do_payout_stakers(validator_stash, era)
		}
```

**File:** substrate/frame/staking/src/lib.rs (L116-123)
```rust
//! Rewards must be claimed for each era before it gets too old by
//! [`HistoryDepth`](`Config::HistoryDepth`) using the `payout_stakers` call. Any account can call
//! `payout_stakers`, which pays the reward to the validator as well as its nominators. Only
//! [`Config::MaxExposurePageSize`] nominator rewards can be claimed in a single call. When the
//! number of nominators exceeds [`Config::MaxExposurePageSize`], then the exposed nominators are
//! stored in multiple pages, with each page containing up to [`Config::MaxExposurePageSize`]
//! nominators. To pay out all nominators, `payout_stakers` must be called once for each available
//! page. Paging exists to limit the i/o cost to mutate storage for each nominator's account.
```
