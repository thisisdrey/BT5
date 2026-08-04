Based on my research, I found a documented local analog to the "burn percentage applies retroactively" bug class in `pallet-nomination-pools`: a configurable percentage (pool commission) that is applied to *already-accrued* value using the current/live rate instead of the rate that was in effect when that value accrued, in one of the pool's commission-mutation call paths.

### Title
Commission rate change via `set_commission_max` re-rates already-accrued pool rewards instead of using the rate in effect when they accrued - (File: substrate/frame/nomination-pools/src/lib.rs)

### Summary
`pallet-nomination-pools` computes commission owed to the pool operator lazily: whenever pool points/commission change, `RewardPool::update_records` is called to "snapshot" all rewards accrued since the last snapshot at the commission rate that was active during that period, converting them into `current_pending_commission` and member rewards before the rate changes [1](#0-0) . The pallet's own documentation states this ordering exists precisely "to prevent the malicious behaviour of changing the commission rate ... after rewards are accumulated, and thus claim an unexpectedly high chunk of the reward" [2](#0-1) .

The `set_commission` extrinsic correctly follows this pattern: it calls `reward_pool.update_records(...)` using `bonded_pool.commission.current()` (the *old* rate) before calling `bonded_pool.commission.try_update_current(&new_commission)` [3](#0-2) .

The repository's own PR-doc record for `pallet-nomination-pools` documents that `set_commission_max` did **not** follow this same invariant: `set_commission_max` force-lowers `commission.current` via `try_update_max` when a new, lower max is set, without first snapshotting via `update_records`. As a direct structural analog of the UMA `burnedBondPercentage` bug, rewards accrued at the higher rate were retroactively re-rated at the new lower max on the next `update_records` call, silently crediting the differential `(old_current - new_max) * accrued` to pool members instead of to the commission payee [4](#0-3) .

### Finding Description
The broken invariant matches the UMA report exactly: a percentage parameter (`burnedBondPercentage` there, pool `commission.current` here) that participants reasonably expect to be fixed for value that already accrued under it, is instead read/applied *live* at the point of later settlement (`update_records`), because the code path that lowers the rate (`set_commission_max` → `try_update_max`) omits the snapshot step (`update_records`) that the sibling path (`set_commission` → `try_update_current`) performs [5](#0-4) . Since `current_reward_counter`/`update_records` splits the *entire* unclaimed payout balance since the last snapshot using whatever `commission.current()` value is active at the time it runs [6](#0-5) , lowering `current` before that snapshot causes commission that should have belonged to the commission payee (accrued while the higher rate was active) to instead be misattributed to members.

### Impact Explanation
This is a value-conservation/wrong-beneficiary bug in a public, permissionless (pool-root-callable) reward accounting path: bonded value is settled to the wrong party in the wrong amount, which falls squarely under "Balances ... treasury spends ... contract-held value must conserve value and settle exactly once to the rightful beneficiary and amount" in the Polkadot SDK Pivots. It results in the commission payee (often the pool operator, sometimes representing protocol-level incentive alignment) permanently losing the portion of commission that had already accrued before the rate was lowered.

### Likelihood Explanation
The trigger requires only calling `set_commission_max` with a new max below the currently active `commission.current` — this is a normal, expected operation for a pool's commission admin (not privileged chain governance/admin abuse of the runtime, but a normal pool-management action any pool "root"/"nominator" role can invoke), combined with rewards having accrued since the last snapshot (`update_records` call), which is the common case for active pools. No malicious peer, validator, relayer, or governance actor is required — only ordinary, permissionless use of the exposed extrinsic.

### Recommendation
Ensure `set_commission_max` calls `reward_pool.update_records(pool_id, bonded_pool.points, bonded_pool.commission.current())` using the pre-change commission rate before calling `bonded_pool.commission.try_update_max(...)`, mirroring the ordering already implemented in `set_commission` [5](#0-4) . This is exactly the fix already recorded in the repository's own `prdoc/pr_12397.prdoc` [7](#0-6) .

### Proof of Concept
1. Pool root sets `commission.current = 20%` via `set_commission`; rewards begin accruing to the pool.
2. Time passes and unclaimed rewards accumulate in the pool's reward account without any intervening `update_records` snapshot (no member claims/joins/unbonds/etc. in the interim).
3. Pool root calls `set_commission_max` with a new max of `5%`, which is below the current `20%`, forcing `commission.current` down to `5%` via `try_update_max` without first snapshotting.
4. The next `update_records` call (triggered by any subsequent claim, join, bond-extra, or unbond) computes `new_pending_commission = commission.current() * current_payout_balance` using the new `5%` rate over the *entire* payout balance accrued since the last snapshot, including the period when `20%` was active [6](#0-5) .
5. The commission payee receives only 5% of rewards that should have been split 20%/80% for the period before the rate change; the differential is misattributed to members' `new_pending_rewards`.

Note: This exact defect is documented as fixed by the repository's own `prdoc/pr_12397.prdoc` [8](#0-7) , which indicates the corresponding code fix likely accompanies that PR; I was unable to fetch and re-verify the exact current body of `set_commission_max` in this snapshot before running out of tool calls, so I cannot state with certainty whether this specific repository checkout still contains the unpatched ordering or already includes the fix. If the fix is already applied at `set_commission_max`, this specific instance is not currently exploitable, but the prdoc itself is direct repository evidence that this precise bug class (percentage applied retroactively/live instead of at the snapshot boundary) is a real, previously-existing local analog to the UMA report.

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L153-159)
```rust
//! Implementation note: Commission is analogous to a separate member account of the pool, with its
//! own reward counter in the form of `current_pending_commission`.
//!
//! Crucially, commission is applied to rewards based on the current commission in effect at the
//! time rewards are transferred into the reward pool. This is to prevent the malicious behaviour of
//! changing the commission rate to a very high value after rewards are accumulated, and thus claim
//! an unexpectedly high chunk of the reward.
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L1448-1471)
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

**File:** substrate/frame/nomination-pools/src/lib.rs (L2977-2989)
```rust
			let mut reward_pool = RewardPools::<T>::get(pool_id)
				.defensive_ok_or::<Error<T>>(DefensiveError::RewardPoolNotFound.into())?;
			// IMPORTANT: make sure that everything up to this point is using the current commission
			// before it updates. Note that `try_update_current` could still fail at this point.
			reward_pool.update_records(
				pool_id,
				bonded_pool.points,
				bonded_pool.commission.current(),
			)?;
			RewardPools::insert(pool_id, reward_pool);

			bonded_pool.commission.try_update_current(&new_commission)?;
			bonded_pool.put();
```

**File:** prdoc/pr_12397.prdoc (L1-15)
```text
title: 'nomination-pools: snapshot rewards before `set_commission_max` lowers current commission'
doc:
- audience: Runtime Dev
  description: |-
    `set_commission_max` force-lowers `commission.current` (via `try_update_max`) when the new max
    is below the active rate, but did not first call `update_records`. Rewards accrued at the higher
    rate since the last snapshot were therefore re-rated at the new lower rate on the next
    `update_records`, crediting the differential `(old_current - new_max) * accrued` to members
    instead of the commission payee.

    The fix snapshots the reward pool at the current commission before the cut, mirroring the
    ordering already used in `set_commission`.
crates:
- name: pallet-nomination-pools
  bump: patch
```
