## Analysis

The reported bug's core invariant: **value can enter a reward-accounting system without being credited into the accumulator that gates payout, because the accumulator update is skipped when the "denominator" (total supply / total staked) is zero — leaving the deposited value unattributed and effectively lost to whoever eventually drains the pot.**

The closest local analog is in `pallet-asset-rewards` (`substrate/frame/asset-rewards/src/lib.rs`).

### Title
Unattributed reward-token deposits into a zero-stake pool are permanently lost to the pool admin - (File: `substrate/frame/asset-rewards/src/lib.rs`)

### Summary
`deposit_reward_tokens` is a public, unprivileged extrinsic that lets *any* signed account transfer reward-asset tokens into a pool's holding account, with no check that the pool currently has any staked tokens. Reward accrual, however, is computed in `reward_per_token()`, which explicitly skips updating `reward_per_token_stored` whenever `total_tokens_staked` is zero. Any tokens deposited during a zero-stake window are therefore never reflected in the pool's reward accumulator and can never be attributed to a staker. If the pool's `PoolStakers` map is empty at that time (which is the normal state when `total_tokens_staked == 0`), the pool admin can immediately call `cleanup_pool`, which sweeps the *entire* pool balance — including the unattributed deposit — to themselves, not to the original depositor.

### Finding Description
`reward_per_token` short-circuits when there is no stake: [1](#0-0) 

`deposit_reward_tokens` performs the transfer with no guard on `total_tokens_staked`: [2](#0-1) 

`cleanup_pool` only requires that `PoolStakers` be empty — it does not check whether unattributed reward-asset balance exists that was deposited by a third party rather than by the admin — and sends the *full* pool balance to the admin: [3](#0-2) 

This is structurally identical to the EsEMBR pattern: a public entry point accepts value into a pool whose distribution math is gated by a "total stake/supply" denominator; when that denominator is zero, the value is silently excluded from the accounting that would let it be claimed by the intended beneficiaries (stakers), and it ends up capturable by an unrelated party (here, the admin) instead of being refunded to the depositor.

### Impact Explanation
Any unprivileged account calling `deposit_reward_tokens` on a pool with `total_tokens_staked == 0` (e.g. right after `create_pool`, or after all stakers have fully `unstake`d) has their reward-asset tokens permanently unattributed to any staker. If no one stakes afterward, the pool admin can call `cleanup_pool` and claim the entire balance for themselves — a value-conservation violation where funds are diverted from the depositor to the admin without the depositor's consent, and with no compensating credit ever recorded. This falls under "permanent user-fund lock" / value not settling to the rightful beneficiary.

### Likelihood Explanation
This requires no privileged actor, malicious validator, or governance action — just an ordinary user calling a public extrinsic (`deposit_reward_tokens`) on a pool that is in (or later reaches) a zero-stake state, which is a normal and easily reachable condition (e.g. immediately after pool creation, or transiently between stakers). The admin only needs to act rationally (call `cleanup_pool` once stakers list is empty) to realize the gain.

### Recommendation
- `deposit_reward_tokens` should either reject deposits when `total_tokens_staked.is_zero()`, or immediately fold the deposited amount into the pool's reward accounting (e.g., increase `reward_per_token_stored` proportionally, or track it as un-emitted principal that carries forward once staking resumes) rather than letting it fall through to `cleanup_pool`.
- `cleanup_pool` should distinguish reward-asset balance attributable to the admin's originally funded emission schedule from balance contributed by third parties, or simply disallow cleanup while there is unattributed reward-asset balance beyond what emission accounting expects.

### Proof of Concept
1. `CreatePoolOrigin` account calls `create_pool` for staked asset `S` / reward asset `R`; pool starts with `total_tokens_staked = 0`.
2. Attacker (or any user) calls `deposit_reward_tokens(pool_id, amount)`, transferring `amount` of `R` into `pool_info.account`. No error occurs because there's no zero-stake guard.
3. Since `PoolStakers` is empty (`stakers.is_none()` holds — see `cleanup_pool` line 703), the admin calls `cleanup_pool(pool_id)`.
4. `cleanup_pool` computes `pool_balance` via `reducible_balance` on `pool_info.account` (which now includes the attacker's deposited `amount`) and transfers all of it to `pool_info.admin`, per lines 706-718.
5. The depositor's tokens are gone, credited entirely to the admin, with no record in `reward_per_token_stored` ever reflecting the deposit. [4](#0-3)

### Citations

**File:** substrate/frame/asset-rewards/src/lib.rs (L672-688)
```rust
		#[pallet::call_index(7)]
		pub fn deposit_reward_tokens(
			origin: OriginFor<T>,
			pool_id: PoolId,
			amount: T::Balance,
		) -> DispatchResult {
			let caller = ensure_signed(origin)?;
			let pool_info = Pools::<T>::get(pool_id).ok_or(Error::<T>::NonExistentPool)?;
			T::Assets::transfer(
				pool_info.reward_asset_id,
				&caller,
				&pool_info.account,
				amount,
				Preservation::Preserve,
			)?;
			Ok(())
		}
```

**File:** substrate/frame/asset-rewards/src/lib.rs (L696-718)
```rust
		#[pallet::call_index(8)]
		pub fn cleanup_pool(origin: OriginFor<T>, pool_id: PoolId) -> DispatchResult {
			let who = ensure_signed(origin)?;

			let pool_info = Pools::<T>::get(pool_id).ok_or(Error::<T>::NonExistentPool)?;
			ensure!(pool_info.admin == who, BadOrigin);

			let stakers = PoolStakers::<T>::iter_key_prefix(pool_id).next();
			ensure!(stakers.is_none(), Error::<T>::NonEmptyPool);

			let pool_balance = T::Assets::reducible_balance(
				pool_info.reward_asset_id.clone(),
				&pool_info.account,
				Preservation::Expendable,
				Fortitude::Polite,
			);
			T::Assets::transfer(
				pool_info.reward_asset_id,
				&pool_info.account,
				&pool_info.admin,
				pool_balance,
				Preservation::Expendable,
			)?;
```

**File:** substrate/frame/asset-rewards/src/lib.rs (L787-792)
```rust
		pub(super) fn reward_per_token(
			pool_info: &PoolInfoFor<T>,
		) -> Result<T::Balance, DispatchError> {
			if pool_info.total_tokens_staked.is_zero() {
				return Ok(pool_info.reward_per_token_stored);
			}
```
