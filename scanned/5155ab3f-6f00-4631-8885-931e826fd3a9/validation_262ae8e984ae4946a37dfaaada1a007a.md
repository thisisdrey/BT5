### Title
Reward-pool balance computed from raw account balance allows griefing of reward-counter precision via direct donation - ([File: substrate/frame/nomination-pools/src/lib.rs])

### Summary
`RewardPool::current_balance` computes a pool's claimable-reward balance by reading the reward account's live `reducible_balance` directly, instead of tracking a delta of funds specifically attributable to staking rewards. This mirrors the reported `P2pLendingProxy.withdraw` bug class: an accounting value that should represent "funds legitimately earned by this operation" is instead read from the current on-chain balance of an account, which is externally, permissionlessly top-uppable by anyone via an ordinary `transfer`.

### Finding Description
`RewardPool::current_balance` in [1](#0-0)  is defined as:

```rust
fn current_balance(id: PoolId) -> BalanceOf<T> {
    T::Currency::reducible_balance(
        &Pallet::<T>::generate_reward_account(id),
        Preservation::Expendable,
        Fortitude::Polite,
    )
}
```

This value is fed directly into `current_reward_counter` [2](#0-1)  as `balance`, from which `current_payout_balance` and thus `new_pending_rewards`/`new_pending_commission` are derived. It is also used in `update_records` [3](#0-2)  to set `last_recorded_total_payouts`.

The `generate_reward_account(id)` address is a deterministic, ordinary `AccountId` (not access-controlled) — any account holder can call `Balances::transfer` (or the equivalent fungible transfer) to send funds directly into it, exactly the same "attacker sends tokens directly to the target account before/around the accounting read" pattern described in the external report for `P2pLendingProxy`. Because `current_balance` is a raw balance read rather than a value reconstructed purely from tracked reward inflows (`total_rewards_claimed`/`total_commission_claimed`/`last_recorded_total_payouts`), any inbound transfer to the reward account is indistinguishable from a legitimate staking-reward payout and is immediately absorbed into `current_payout_balance`.

Existing guards do not prevent this:
- There is no filter or hold preventing arbitrary transfers into the reward account.
- `update_records`/`current_reward_counter` have no way to distinguish "reward earned from staking" balance from "balance sent by an arbitrary account."
- The only protections in the codebase are `try-state`/defensive checks (`do_try_state`, `check_ed_imbalance`) which are diagnostic, not preventive, and only run under `try-runtime`/tests [4](#0-3) .

### Impact Explanation
Because `new_pending_rewards`/`new_pending_commission` (and thus each member's `pending_rewards` via `pending_rewards(current_reward_counter)`) are directly proportional to `current_balance`, a donation to the reward account is instantly convertible into "claimable reward" that gets distributed pro-rata to whichever points existed in the bonded pool at the time `current_reward_counter`/`update_records` is next evaluated. Because `update_records` is invoked on point-changing operations (`join`, `bond_extra`, `unbond`), the precise timing of a donation relative to a `join`/`bond_extra` transaction determines who captures the donated value — enabling reward-counter/precision griefing and unpredictable redistribution of funds that were never legitimately earned by the pool's staking activity. This corrupts the core invariant that `RewardPool` balance should represent only real staking payouts (documented explicitly in the pallet's own module docs at [5](#0-4) : "a reward pool also tracks its outstanding and claimed rewards as counters"), and can desynchronize the reward pool's `try-state` invariant checked at [4](#0-3) .

### Likelihood Explanation
The attack requires no privileged role, no malicious validator/collator, and no admin/governance action — any unprivileged account can send a plain balance transfer to the deterministic reward account address (`generate_reward_account(id)` is derivable off-chain for any pool id) and then trigger (or wait for) a point-changing extrinsic (`join`, `bond_extra`, `unbond`, `claim_payout`) to realize the redistribution. This satisfies the "public underpriced work" / unauthorized-value-redistribution class in scope, since the mechanism is entirely reachable through public dispatchables.

### Recommendation
Do not derive reward-pool payout balances from a raw `reducible_balance` read of the reward account. Instead:
- Track reward inflows explicitly (e.g., only credit `current_payout_balance` for balance increases attributable to recorded staking-reward deposits), or
- Snapshot the reward-account balance immediately before and after the staking reward payout event and use the delta, analogous to the `P2pLendingProxy` fix recommendation of capturing balance before/after and computing the difference, rather than trusting the live balance which can be manipulated by third-party transfers at arbitrary times.

### Proof of Concept
1. Create nomination pool `P` with member `A` holding all points.
2. Determine `reward_account = NominationPools::generate_reward_account(P)` (deterministic, computable off-chain).
3. Attacker (unprivileged) calls `Balances::transfer_keep_alive(reward_account, X)` to donate `X` tokens directly into the reward account — no staking reward was ever earned.
4. Immediately after, attacker (or an accomplice) calls `Pools::join(pool_id=P, amount=large)` right before any pending reward is recorded, or calls `Pools::claim_payout` as an existing member.
5. `RewardPool::current_balance(P)` returns the inflated balance (original + `X`), which flows into `current_reward_counter`/`update_records`, causing `X` to be distributed as if it were legitimate staking reward, pro-rata to whichever points exist in the bonded pool at that moment — a value never actually earned through staking, and distributable to accounts that did not contribute it.

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L263-276)
```rust
//! ### Reward pool
//!
//! When a pool is first bonded it sets up a deterministic, inaccessible account as its reward
//! destination. This reward account combined with `RewardPool` compose a reward pool.
//!
//! Reward pools are completely separate entities to bonded pools. Along with its account, a reward
//! pool also tracks its outstanding and claimed rewards as counters, in addition to pending and
//! claimed commission. These counters are updated with `RewardPool::update_records`. The current
//! reward counter of the pool (the total outstanding rewards, in points) is also callable with the
//! `RewardPool::current_reward_counter` method.
//!
//! See [this link](https://hackmd.io/PFGn6wI5TbCmBYoEA_f2Uw) for an in-depth explanation of the
//! reward pool mechanism.
//!
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L1408-1445)
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
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L1450-1465)
```rust
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
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L1514-1523)
```rust
	/// Current free balance of the reward pool.
	///
	/// This is sum of all the rewards that are claimable by pool members.
	fn current_balance(id: PoolId) -> BalanceOf<T> {
		T::Currency::reducible_balance(
			&Pallet::<T>::generate_reward_account(id),
			Preservation::Expendable,
			Fortitude::Polite,
		)
	}
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
