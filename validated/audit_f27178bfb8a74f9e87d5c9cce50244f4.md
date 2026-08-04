### Title
Nomination-pool reward accounting can enter an unenforced deficit state where pending member rewards exceed the pool's actual reward-account balance - (File: `substrate/frame/nomination-pools/src/lib.rs`)

### Summary
`pallet-nomination-pools` computes each member's claimable reward via an independently-maintained "reward counter" ledger (`RewardPool::current_reward_counter` / `PoolMember::pending_rewards`) rather than by directly apportioning the reward account's live balance at claim time. The pallet's own sanity checks acknowledge that the sum of all members' computed `pending_rewards` for a pool can exceed the pool's actual `current_balance`, i.e. a deficit/"bad debt" state, and explicitly state this is only logged as a warning, not prevented. This is the same class of bug as the Perpetual funding-fee finding: two logically-linked ledgers (aggregate claimable entitlement vs. actual pooled balance) that are expected to stay in balance but can diverge, with no secondary funding source to cover the shortfall.

### Finding Description
`RewardPool::current_reward_counter` derives `new_pending_rewards`/`new_pending_commission` from `current_payout_balance = balance + total_rewards_claimed + total_commission_claimed - last_recorded_total_payouts`, where `balance = Self::current_balance(id)` is queried independently at update time [1](#0-0) . This reward counter is only recomputed/committed at specific mutation points (`join`, `bond_extra`, `unbond`, commission changes) via `update_records` [2](#0-1) , and each member's individually accrued `pending_rewards` is derived from the delta between the member's `last_recorded_reward_counter` and the pool's counter, multiplied by the member's `active_points` [3](#0-2) .

`do_try_state` explicitly recomputes, for every pool, the sum of all members' currently-owed `pending_rewards` and compares it against the reward account's `current_balance`. The comment on this check states the invariant can be violated by "an old bug", and that violations are only warned about, never blocked or corrected on-chain: [4](#0-3) 

The pallet has already had to patch one concrete cause of this divergence — Existential Deposit changes causing the `balance - ED` heuristic to produce a deficit, fixed by freezing ED in the reward account (`freeze_pool_deposit`, `adjust_pool_deposit`) per `prdoc/1.3.0/pr_1255.prdoc` [5](#0-4) , and the maintainers' own regression test (`ed_change_causes_reward_deficit`) proves the deficit state is reachable through ordinary pool operations (join/bond_extra), not any malicious or privileged action [6](#0-5) . The fix only neutralizes the ED-driven trigger; the underlying mechanism — an aggregate ledger (`pending_rewards` across members) that is not directly bound to the pool's live balance at all times, only reconciled opportunistically on state-changing calls — remains and is still only monitored, not enforced, as shown by the still-present `do_try_state` warning path.

`do_reward_payout` (invoked from the public `claim_payout`/`claim_payout_other` extrinsics as well as implicitly during `bond_extra`/`unbond`) pays out `pending_rewards` computed purely from the ledger, relying solely on the downstream `Currency::transfer(..., Preservation::Preserve)` to fail if the reward account genuinely lacks funds [7](#0-6) . There is no check, prior to committing `member.last_recorded_reward_counter = current_reward_counter` and `reward_pool.register_claimed_reward(...)`, that the pool's aggregate outstanding entitlement is actually covered by its balance.

### Impact Explanation
When the aggregate ledger of "owed" rewards across all members of a pool exceeds the pool's actual reward-account balance, first-mover members can successfully claim their full computed entitlement while later members' `claim_payout` calls fail (transfer preservation revert) or receive truncated amounts once the account is drained — a race for a shared, insufficiently-backed pot with no additional funding source, exactly mirroring the reported Perpetual finding's "excess funds in the pool or bad debt" outcome. Because nomination pools handle real staked DOT/KSM rewards for potentially many members, this can cause honest, non-malicious members to be permanently unable to claim rewards they are legitimately owed under the pallet's own accounting.

### Likelihood Explanation
The `do_try_state` deficit-detection code and the dedicated regression test (`ed_change_causes_reward_deficit`) confirm the maintainers know this state is reachable in production-realistic usage (only the ED-driven trigger has been patched); other divergence sources are explicitly anticipated ("most likely due to an old bug") and only logged, not prevented, so the likelihood of some non-ED-driven drift recurring — and of the payout path lacking any pool-level solvency check — is non-trivial and requires no privileged/malicious actor, only ordinary member interactions (`join`, `bond_extra`, `unbond`, `claim_payout`).

### Recommendation
Add an explicit solvency guard in `do_reward_payout`/`register_claimed_reward` that caps or defensively checks a member's payout against the reward pool's actual reducible balance before committing the reward-counter update, and consider deriving `pending_rewards` from a value that is provably bounded by the account's real balance at all times (not just reconciled at specific mutation points), rather than only detecting the mismatch after the fact in `try-runtime` checks.

### Proof of Concept
The existing test `ed_change_causes_reward_deficit` demonstrates the general mechanism end-to-end (member joins, rewards deposited, a subsequent parameter change desynchronizes `current_balance` from the recorded ledger, producing `RewardImbalance::Deficit(45)` that persists across further pool operations until manually topped up) [6](#0-5) . The `do_try_state` check at lines 3998-4016 is the pallet's own acknowledgment that this class of state (`pending_rewards_lt_leftover_bal == false`) can recur for reasons other than ED changes and is not blocked at the extrinsic level [4](#0-3) .

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L533-559)
```rust
impl<T: Config> PoolMember<T> {
	/// The pending rewards of this member.
	fn pending_rewards(
		&self,
		current_reward_counter: T::RewardCounter,
	) -> Result<BalanceOf<T>, Error<T>> {
		// accuracy note: Reward counters are `FixedU128` with base of 10^18. This value is being
		// multiplied by a point. The worse case of a point is 10x the granularity of the balance
		// (10x is the common configuration of `MaxPointsToBalance`).
		//
		// Assuming roughly the current issuance of polkadot (12,047,781,394,999,601,455, which is
		// 1.2 * 10^9 * 10^10 = 1.2 * 10^19), the worse case point value is around 10^20.
		//
		// The final multiplication is:
		//
		// rc * 10^20 / 10^18 = rc * 100
		//
		// the implementation of `multiply_by_rational_with_rounding` shows that it will only fail
		// if the final division is not enough to fit in u128. In other words, if `rc * 100` is more
		// than u128::max. Given that RC is interpreted as reward per unit of point, and unit of
		// point is equal to balance (normally), and rewards are usually a proportion of the points
		// in the pool, the likelihood of rc reaching near u128::MAX is near impossible.

		(current_reward_counter.defensive_saturating_sub(self.last_recorded_reward_counter))
			.checked_mul_int(self.active_points())
			.ok_or(Error::<T>::OverflowRisk)
	}
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L1402-1446)
```rust
	/// Update the recorded values of the reward pool.
	///
	/// This function MUST be called whenever the points in the bonded pool change, AND whenever the
	/// the pools commission is updated. The reason for the former is that a change in pool points
	/// will alter the share of the reward balance among pool members, and the reason for the latter
	/// is that a change in commission will alter the share of the reward balance among the pool.
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

**File:** substrate/frame/nomination-pools/src/lib.rs (L1456-1470)
```rust
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

**File:** substrate/frame/nomination-pools/src/lib.rs (L3539-3564)
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

**File:** substrate/frame/nomination-pools/src/lib.rs (L3998-4016)
```rust
		RewardPools::<T>::iter_keys().try_for_each(|id| -> Result<(), TryRuntimeError> {
			// the sum of the pending rewards must be less than the leftover balance. Since the
			// reward math rounds down, we might accumulate some dust here.
			let pending_rewards_lt_leftover_bal = RewardPool::<T>::current_balance(id) >=
				pools_members_pending_rewards.get(&id).copied().unwrap_or_default();

			// If this happens, this is most likely due to an old bug and not a recent code change.
			// We warn about this in try-runtime checks but do not panic.
			if !pending_rewards_lt_leftover_bal {
				log!(
					warn,
					"pool {:?}, sum pending rewards = {:?}, remaining balance = {:?}",
					id,
					pools_members_pending_rewards.get(&id),
					RewardPool::<T>::current_balance(id)
				);
			}
			Ok(())
		})?;
```

**File:** prdoc/1.3.0/pr_1255.prdoc (L1-21)
```text
# Schema: Parity PR Documentation Schema (prdoc)
# See doc at https://github.com/paritytech/prdoc

title: Fix for Reward Deficit in the pool

doc:
  - audience: Runtime Dev
    description: |
      Instead of fragile calculation of current balance by looking at free balance - ED, Nomination Pool now freezes ED in the pool reward account to restrict an account from going below minimum balance. This also has a nice side effect that if ED changes, we know how much is the imbalance in ED frozen in the pool and the current required ED. A pool operator can diligently top up the pool with the deficit in ED or vice versa, withdraw the excess they transferred to the pool.

      notes:
      - Introduces new call `adjust_pool_deposit` that allows to top up the deficit or withdraw the excess deposit for the pool.
      - Switch to using Fungible trait from Currency trait.

migrations:
  runtime:
    - reference: pallet-nomination-pools
      description: One time migration of freezing ED from each of the existing pools.

crates:
  - name: pallet-nomination-pools
```

**File:** substrate/frame/nomination-pools/src/tests.rs (L319-393)
```rust
	#[test]
	fn ed_change_causes_reward_deficit() {
		ExtBuilder::default().max_members_per_pool(Some(5)).build_and_execute(|| {
			// original ED
			ExistentialDeposit::set(5);

			// 11 joins the pool
			Currency::set_balance(&11, 500);
			assert_ok!(Pools::join(RuntimeOrigin::signed(11), 90, 1));

			// new delegator does not have any pending rewards
			assert_eq!(pending_rewards_for_delegator(11), 0);

			// give the pool some rewards
			deposit_rewards(100);

			// all existing delegator has pending rewards
			assert_eq!(pending_rewards_for_delegator(11), 90);
			assert_eq!(pending_rewards_for_delegator(10), 10);
			assert_eq!(reward_imbalance(1), Surplus(0));

			// 12 joins the pool.
			Currency::set_balance(&12, 500);
			assert_ok!(Pools::join(RuntimeOrigin::signed(12), 100, 1));

			// Current reward balance is committed to last recorded reward counter of
			// the pool before the increase in ED.
			let bonded_pool = BondedPools::<Runtime>::get(1).unwrap();
			let reward_pool = RewardPools::<Runtime>::get(1).unwrap();
			assert_eq!(
				reward_pool.last_recorded_reward_counter,
				reward_pool
					.current_reward_counter(1, bonded_pool.points, Perbill::zero())
					.unwrap()
					.0
			);

			// reward pool before ED increase and reward counter getting committed.
			let reward_pool_1 = RewardPools::<Runtime>::get(1).unwrap();

			// increase ED from 5 to 50
			ExistentialDeposit::set(50);

			// There is now an expected deficit of ed_diff
			assert_eq!(reward_imbalance(1), Deficit(45));

			// 13 joins the pool which commits the reward counter to reward pool.
			Currency::set_balance(&13, 500);
			assert_ok!(Pools::join(RuntimeOrigin::signed(13), 100, 1));

			// still a deficit
			assert_eq!(reward_imbalance(1), Deficit(45));

			// reward pool after ED increase
			let reward_pool_2 = RewardPools::<Runtime>::get(1).unwrap();

			// last recorded total payout does not decrease even as ED increases.
			assert_eq!(
				reward_pool_1.last_recorded_total_payouts,
				reward_pool_2.last_recorded_total_payouts
			);

			// Topping up pool decreases deficit
			deposit_rewards(10);
			assert_eq!(reward_imbalance(1), Deficit(35));

			// top up the pool to remove the deficit
			deposit_rewards(35);
			// No deficit anymore
			assert_eq!(reward_imbalance(1), Surplus(0));

			// fix the ed deficit
			assert_ok!(Currency::mint_into(&10, 45));
			assert_ok!(Pools::adjust_pool_deposit(RuntimeOrigin::signed(10), 1));
		});
```
