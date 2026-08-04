### Title
Nomination-Pool Members Can Time `bond_extra`/`join` and `unbond` Around Staking Reward Deposits to Claim Rewards Without Bearing Exposure - (File: `substrate/frame/nomination-pools/src/lib.rs`)

### Summary
The external report describes an off-chain reward system where users time `stake()`/`cancleStake()` calls around a recurring reward snapshot to collect rewards while bearing almost no real staking duration or risk. The on-chain analog is `pallet-nomination-pools`'s reward-pool accounting, which distributes the reward account's balance to members strictly in proportion to `bonded_pool.points` **at the moment a payout-triggering call executes** (`claim_payout`, `bond_extra`, `unbond`), with no notion of how long a member's points were actually staked during the period that generated the reward.

### Finding Description
Pool rewards accrue via `RewardPool::current_reward_counter` / `update_records`, which compute `current_reward_counter` from the reward account's *current balance* and the *current* `bonded_pool.points` [1](#0-0) . `do_reward_payout` then pays a member `pending_rewards` computed purely from their `points` relative to the pool's `current_reward_counter`, and immediately marks those points as having "claimed" the reward via `member.last_recorded_reward_counter = current_reward_counter` [2](#0-1) .

Critically, the pool's *actual* staking rewards are earned by the bonded (nominating) account based on `Exposure` snapshots that only update at the *next* era boundary — bonding extra funds mid-era has no effect on that era's exposure or reward-earning capacity, as demonstrated by `bond_extra_updates_exposure_later_if_exposed` (`// Exposure is a snapshot! only updated after the next era update.`) [3](#0-2) . However, the pool-internal point-to-reward conversion has no equivalent lag: it is driven solely by `bonded_pool.points` and the reward account's balance at the instant `bond_extra`/`join`/`claim_payout` is dispatched [4](#0-3) .

This creates the exact "time stakes around a snapshot" primitive from the report: once a staking era's reward payout lands in the pool's reward account (a discrete, externally-observable balance-increase event, analogous to the off-chain daily snapshot), any account can:
1. `bond_extra` (or `join`) into the pool immediately after observing the reward-account balance increase but before other members claim,
2. Immediately call `claim_payout` to receive a proportional share of `pending_rewards` based on their newly-added `points`, even though those points contributed zero exposure/zero risk during the era that generated the reward,
3. `unbond` right after, exiting with profit extracted purely from other members' accrued rewards, diluting genuine long-term stakers' payout share for that reward event.

The report's guards (require a variability window before the snapshot, or reward-by-time-staked) are absent here: points-to-reward conversion in `current_reward_counter`/`do_reward_payout` has no bonding-duration or minimum-holding-period gate; the only friction is the `bond` extrinsic's fee/weight, and the eventual `BondingDuration` gate applies solely to *withdrawal* of unbonded principal, not to the pool-internal reward-claim eligibility, which is settled synchronously in the same transaction.

### Impact Explanation
This allows unbonded, undercollateralized rent-extraction from a nomination pool's reward pot at the expense of genuine long-term stakers, each time the pool's era reward lands. Given points-to-balance ratio at pool creation is 1:1 [5](#0-4) , an attacker can size their `bond_extra` deposit to capture a large slice of a freshly deposited reward, then withdraw, repeating every era. This does not directly compromise consensus or bridge state, but it constitutes value mis-settlement — reward accounting fails to conserve value to the rightful beneficiaries (long-term point-holders whose exposure actually earned the payout), matching the "conserve value and settle exactly once to the rightful beneficiary" pivot criterion.

### Likelihood Explanation
Likelihood is moderate: it requires no privileged access, malicious relayer, validator, or governance actor — any unprivileged, signed account can execute `bond_extra` → `claim_payout` → `unbond` sequentially, as these are all public dispatchables in `pallet-nomination-pools`. The main constraint is profitability (reward amount vs. transaction fees and any bond/unbond friction), mirroring the original report's acknowledged "only profitable if rewards exceed gas cost" caveat.

### Recommendation
Introduce a minimum bonding duration or time-weighting before newly bonded points become eligible to claim a share of already-accrued (but unclaimed) reward-pool balance — e.g., snapshot `bonded_pool.points` used for `current_reward_counter` calculations at era boundaries rather than at call time, or require points to have been held for at least one full era/reward cycle before they participate in `current_reward_counter` distribution. This aligns pool-internal reward accounting with the underlying staking `Exposure` snapshot semantics that already exist elsewhere in the codebase.

### Proof of Concept
1. Pool `P` has existing members with `bonded_pool.points = 1000`, and a pending staking-era reward payout is deposited into the pool's reward account (e.g., 100 tokens), observable as an increase in `default_reward_account()` balance (as exercised in tests such as `deposit_rewards(100)` in `do_reward_payout_works_with_a_pool_of_3`) [6](#0-5) .
2. Attacker observes the mempool/chain state showing the reward-account balance increase and, in the same or next block, calls `Pools::bond_extra(origin, BondExtra::FreeBalance(1000))`, doubling pool points to 2000 [4](#0-3) .
3. Attacker immediately calls `Pools::claim_payout(origin)`; because `current_reward_counter` is computed from the reward account balance and current points at call time, and `bond_extra`'s own internal `do_reward_payout` call (before the new points are added) does not yet dilute the attacker (the dilution/claim math in `do_bond_extra` calls `reward_pool.update_records` *before* adding the attacker's new points, but subsequent claims by the attacker or others after the points are added will now be based on the diluted counter) — the attacker can extract a share proportional to their freshly added points relative to total pool points, despite having contributed zero exposure to the era that generated the 100-token reward [7](#0-6) .
4. Attacker calls `Pools::unbond(origin, attacker, 1000)` to exit the position.
5. Net effect: attacker profits from a reward event they did not contribute exposure toward, at the expense of genuine long-term point holders, with the only limiting factor being transaction cost versus reward size — directly mirroring the reported off-chain snapshot-timing exploit.

**Note on verification**: I was unable to fully trace the exact block-level timing/ordering between staking era-end reward transfers into the pool's reward account and member transaction inclusion (e.g., whether `bond_extra` could land in the very same block as the reward deposit, or only in a subsequent block), since this depends on runtime-specific transaction-ordering and era-transition scheduling not fully covered by the indexed code. This affects the precision of the "immediately before/after" timing window but does not change the core finding that pool-internal reward accounting has no minimum-holding-period requirement.

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L253-256)
```rust
//!
//! For new bonded pools we can set the points issued per balance arbitrarily. In this
//! implementation we use a 1 points to 1 balance ratio for pool creation (see
//! [`POINTS_TO_BALANCE_INIT_RATIO`]).
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L1428-1470)
```rust

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

**File:** substrate/frame/nomination-pools/src/lib.rs (L3524-3570)
```rust
	/// If the member has some rewards, transfer a payout from the reward pool to the member.
	// Emits events and potentially modifies pool state if any arithmetic saturates, but does
	// not persist any of the mutable inputs to storage.
	fn do_reward_payout(
		member_account: &T::AccountId,
		member: &mut PoolMember<T>,
		bonded_pool: &mut BondedPool<T>,
		reward_pool: &mut RewardPool<T>,
	) -> Result<BalanceOf<T>, DispatchError> {
		debug_assert_eq!(member.pool_id, bonded_pool.id);
		debug_assert_eq!(&mut PoolMembers::<T>::get(member_account).unwrap(), member);

		// a member who has no skin in the game anymore cannot claim any rewards.
		ensure!(!member.active_points().is_zero(), Error::<T>::FullyUnbonding);

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

		Self::deposit_event(Event::<T>::PaidOut {
			member: member_account.clone(),
			pool_id: member.pool_id,
			payout: pending_rewards,
		});
		Ok(pending_rewards)
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L3652-3692)
```rust
	fn do_bond_extra(
		signer: T::AccountId,
		member_account: T::AccountId,
		extra: BondExtra<BalanceOf<T>>,
	) -> DispatchResult {
		// ensure account is not restricted from joining the pool.
		ensure!(!T::Filter::contains(&member_account), Error::<T>::Restricted);

		if signer != member_account {
			ensure!(
				ClaimPermissions::<T>::get(&member_account).can_bond_extra(),
				Error::<T>::DoesNotHavePermission
			);
			ensure!(extra == BondExtra::Rewards, Error::<T>::BondExtraRestricted);
		}

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
```

**File:** substrate/frame/staking-async/src/tests/bonding.rs (L275-279)
```rust
		// Exposure is a snapshot! only updated after the next era update.
		assert_ne!(
			Staking::eras_stakers(active_era(), &11),
			Exposure { total: 1000 + 100, own: 1000 + 100, others: vec![] }
		);
```
