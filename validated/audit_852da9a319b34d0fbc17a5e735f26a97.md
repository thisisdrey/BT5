### Title
Any staker can permanently zero out reward emission in `pallet-asset-rewards` via cheap per-block interactions, silently advancing `last_update_block` without crediting `reward_per_token_stored` - (File: `substrate/frame/asset-rewards/src/lib.rs`)

### Summary
`pallet-asset-rewards`'s reward accrual mirrors the reported DBR bug class exactly: a public, permissionless entry point (`stake`, `unstake`, `harvest_rewards`) recomputes a per-block-elapsed reward increment via integer division and then unconditionally advances the `last_update_block` "checkpoint" to the current block — even when the computed increment rounds down to zero. An attacker who triggers this update every block (with a large enough `total_tokens_staked` denominator relative to `reward_rate_per_block`) can make every single-block accrual round to zero, permanently discarding that block's reward emission for **all** stakers in the pool while the reward tokens remain unclaimably stuck in the pool account.

### Finding Description
`reward_per_token` computes the accrual as: [1](#0-0) 

```rust
let rewardable_blocks_elapsed: u32 = ... last_block_reward_applicable(expiry_block)
    .ensure_sub(pool_info.last_update_block)? ...;
Ok(pool_info.reward_per_token_stored.ensure_add(
    pool_info.reward_rate_per_block
        .ensure_mul(rewardable_blocks_elapsed.into())?
        .ensure_mul(PRECISION_SCALING_FACTOR.into())?
        .ensure_div(pool_info.total_tokens_staked)?,
)?)
```

`PRECISION_SCALING_FACTOR` is only `4096`, not a large fixed-point scale: [2](#0-1) 

`update_pool_rewards` then unconditionally stores `last_update_block = now` regardless of whether the computed `reward_per_token` actually changed: [3](#0-2) 

This mirrors the DBR pattern precisely: `lastUpdated[user]` was advanced by `accrueDueTokens` even when `accrued == 0`. Here, `last_update_block` is advanced by `update_pool_rewards` even when the reward increment computed for the elapsed window rounds to `0` under integer division.

The update path is reachable by any signed account via three fully public extrinsics that all call `update_pool_and_staker_rewards` → `reward_per_token` → `update_pool_rewards` before doing anything else:
- `stake` (call_index 1): [4](#0-3) 
- `unstake` (call_index 2): [5](#0-4) 
- `harvest_rewards` (call_index 3): [6](#0-5) 

Any of these can be called by an unprivileged staker every block with a trivial-sized stake (e.g. `stake(pool_id, 1)`/`unstake(pool_id, 1)` round-trips, or repeated `harvest_rewards` when the attacker's own accrued reward is 0), since there is no minimum interval enforced and no cost beyond ordinary transaction fees.

Given `rewardable_blocks_elapsed = 1` for a per-block call, the round-down-to-zero condition is:

```
reward_rate_per_block * 1 * 4096 < total_tokens_staked
```

i.e. `total_tokens_staked > reward_rate_per_block * 4096`. Because on-chain asset amounts commonly use 10-18 decimals, `total_tokens_staked` for any pool with meaningful liquidity will vastly exceed `reward_rate_per_block * 4096` for realistic reward rates, making this condition trivially satisfied in practice — unlike the original DBR PoC, which needed careful timing/amount tuning against 365-day scaling, here the scaling factor (4096) is small enough that the rounding-to-zero window is wide and easy to hit for essentially any real pool.

### Impact Explanation
Once an attacker (or even an unintentional pattern of frequent staker interactions) causes `update_pool_rewards` to run every block with a rounding-to-zero increment, `reward_per_token_stored` stops growing while `last_update_block` keeps advancing to the current block. The reward window for each such block is permanently lost — it can never be "made up," because the next update only measures elapsed blocks from the newly-advanced `last_update_block`. This:
- Denies rewards to **all** stakers in the pool (not just the attacker), a durable value-loss/DoS on the intended reward-emission logic of the pallet.
- Leaves the reward asset balance held by the pool's account (`pool_info.account`) permanently unclaimable by any staker, effectively locking funds that the pool admin funded for distribution (`deposit_reward_tokens` / `create_pool`), matching the "permanent user-fund lock" and "runtime bug that compromises intended behavior" impact categories.
- Requires no privileged role, governance, validator, or malicious peer — a single unprivileged signed account, submitting cheap ordinary extrinsics every block, is sufficient.

### Likelihood Explanation
High for any active pool with realistic (18/12/10-decimal) staked-asset amounts and any reward rate not itself scaled to the same magnitude as `total_tokens_staked`. The condition `total_tokens_staked > reward_rate_per_block * 4096` is easily true for ordinary pools, and per-block calls to `stake`/`unstake`/`harvest_rewards` cost only normal transaction fees — no economic infeasibility barrier exists as it did in the original DBR report (which needed sub-cent DOLA amounts and 12-second granularity against a 365-day divisor). No existing guard checks whether the computed reward delta is zero before persisting `last_update_block`.

### Recommendation
- In `update_pool_rewards` (or `reward_per_token`), do not advance `last_update_block` to `now` when the computed `reward_per_token` delta is zero; instead leave `last_update_block` unchanged (or track undistributed remainder) so a future call with sufficient elapsed blocks can still credit the full owed amount.
- Alternatively, accumulate a fractional remainder (as `Perquintill`/`FixedU128` internally) so the rounding error does not silently vanish across repeated small-window updates, analogous to how `pallet-nomination-pools`' `current_reward_counter` uses higher-precision `RewardCounter` types to avoid this class of loss.
- Increase `PRECISION_SCALING_FACTOR` substantially (e.g., to `10^18`-scale fixed point) to shrink the rounding-to-zero window for realistic stake sizes.

### Proof of Concept
1. `create_pool` with `reward_rate_per_block = 100` and stake enough tokens (from any set of stakers) that `total_tokens_staked > 100 * 4096 = 409_600` (trivial for typical decimal-scaled assets, e.g. total staked = `10^18`).
2. Attacker stakes a minimal amount, e.g. `stake(pool_id, 1)`.
3. Each subsequent block, attacker calls `unstake(pool_id, 0)`-equivalent (or repeats `stake`/`unstake` of `1`) — or simply calls `harvest_rewards(pool_id, None)` every block; each call triggers `update_pool_and_staker_rewards` → `reward_per_token`.
4. For `rewardable_blocks_elapsed = 1`, the increment `100 * 1 * 4096 / total_tokens_staked` (with `total_tokens_staked = 10^18`) computes to `0` via integer division; `reward_per_token_stored` is unchanged, but `last_update_block` is set to the current block via `update_pool_rewards`.
5. Repeating step 3-4 every block for the pool's entire duration results in `reward_per_token_stored` never advancing meaningfully, even though `reward_rate_per_block * total_elapsed_blocks` (the amount the admin funded/intended to distribute) is nonzero and large — that reward remains stuck in `pool_info.account` and is never credited to any staker. [7](#0-6) [3](#0-2)

### Citations

**File:** substrate/frame/asset-rewards/src/lib.rs (L117-118)
```rust
/// Multiplier to maintain precision when calculating rewards.
pub(crate) const PRECISION_SCALING_FACTOR: u16 = 4096;
```

**File:** substrate/frame/asset-rewards/src/lib.rs (L472-481)
```rust
		#[pallet::call_index(1)]
		pub fn stake(origin: OriginFor<T>, pool_id: PoolId, amount: T::Balance) -> DispatchResult {
			let staker = ensure_signed(origin)?;

			// Always start by updating staker and pool rewards.
			let pool_info = Pools::<T>::get(pool_id).ok_or(Error::<T>::NonExistentPool)?;
			let staker_info = PoolStakers::<T>::get(pool_id, &staker).unwrap_or_default();
			let (mut pool_info, mut staker_info) =
				Self::update_pool_and_staker_rewards(&pool_info, &staker_info)?;

```

**File:** substrate/frame/asset-rewards/src/lib.rs (L513-530)
```rust
		#[pallet::call_index(2)]
		pub fn unstake(
			origin: OriginFor<T>,
			pool_id: PoolId,
			amount: T::Balance,
			staker: Option<T::AccountId>,
		) -> DispatchResult {
			let caller = ensure_signed(origin)?;
			let staker = staker.unwrap_or(caller.clone());

			// Always start by updating the pool rewards.
			let pool_info = Pools::<T>::get(pool_id).ok_or(Error::<T>::NonExistentPool)?;
			let now = T::BlockNumberProvider::current_block_number();
			ensure!(now > pool_info.expiry_block || caller == staker, BadOrigin);

			let staker_info = PoolStakers::<T>::get(pool_id, &staker).unwrap_or_default();
			let (mut pool_info, mut staker_info) =
				Self::update_pool_and_staker_rewards(&pool_info, &staker_info)?;
```

**File:** substrate/frame/asset-rewards/src/lib.rs (L568-585)
```rust
		#[pallet::call_index(3)]
		pub fn harvest_rewards(
			origin: OriginFor<T>,
			pool_id: PoolId,
			staker: Option<T::AccountId>,
		) -> DispatchResult {
			let caller = ensure_signed(origin)?;
			let staker = staker.unwrap_or(caller.clone());

			// Always start by updating the pool and staker rewards.
			let pool_info = Pools::<T>::get(pool_id).ok_or(Error::<T>::NonExistentPool)?;
			let now = T::BlockNumberProvider::current_block_number();
			ensure!(now > pool_info.expiry_block || caller == staker, BadOrigin);

			let staker_info =
				PoolStakers::<T>::get(pool_id, &staker).ok_or(Error::<T>::NonExistentStaker)?;
			let (pool_info, mut staker_info) =
				Self::update_pool_and_staker_rewards(&pool_info, &staker_info)?;
```

**File:** substrate/frame/asset-rewards/src/lib.rs (L775-784)
```rust
		pub fn update_pool_rewards(
			pool_info: &PoolInfoFor<T>,
			reward_per_token: T::Balance,
		) -> Result<PoolInfoFor<T>, DispatchError> {
			let mut new_pool_info = pool_info.clone();
			new_pool_info.last_update_block = T::BlockNumberProvider::current_block_number();
			new_pool_info.reward_per_token_stored = reward_per_token;

			Ok(new_pool_info)
		}
```

**File:** substrate/frame/asset-rewards/src/lib.rs (L794-810)
```rust
			let rewardable_blocks_elapsed: u32 =
				match Self::last_block_reward_applicable(pool_info.expiry_block)
					.ensure_sub(pool_info.last_update_block)?
					.try_into()
				{
					Ok(b) => b,
					Err(_) => return Err(Error::<T>::BlockNumberConversionError.into()),
				};

			Ok(pool_info.reward_per_token_stored.ensure_add(
				pool_info
					.reward_rate_per_block
					.ensure_mul(rewardable_blocks_elapsed.into())?
					.ensure_mul(PRECISION_SCALING_FACTOR.into())?
					.ensure_div(pool_info.total_tokens_staked)?,
			)?)
		}
```
