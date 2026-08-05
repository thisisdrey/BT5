### Title
Permanent underflow-triggered revert freezes staked funds in expired `asset-rewards` pools - (File: `substrate/frame/asset-rewards/src/lib.rs`)

### Summary
The `pallet-asset-rewards` reward-accounting logic mirrors the Munchables bug class: a "last update" cursor is advanced unconditionally on every interaction, but a later checked subtraction assumes that cursor can never exceed a fixed upper bound (`expiry_block`). Once a pool has expired and any account interacts with it (stake, unstake, harvest), the stored cursor is pushed past the expiry bound, after which every subsequent reward computation performs a checked subtraction that always fails, permanently reverting `unstake`/`harvest_rewards`/`stake` for that pool and freezing all staked tokens until a privileged admin extends the expiry.

### Finding Description
`reward_per_token` computes the number of "rewardable" blocks elapsed since the pool was last updated: [1](#0-0) 

`last_block_reward_applicable` clamps "now" to the pool's `expiry_block`: [2](#0-1) 

`update_pool_rewards`, called from every stake/unstake/harvest path via `update_pool_and_staker_rewards`, unconditionally advances `last_update_block` to the *current* block number regardless of whether the pool has already expired: [3](#0-2) 

Sequence of events:
1. Pool expires at block `E` (`expiry_block = E`).
2. After `E`, any user calls `stake`, `unstake`, or `harvest_rewards`. At this point `reward_per_token` still computes correctly because `last_update_block <= E` (pool was last touched before expiry), so `last_block_reward_applicable(E) - last_update_block` doesn't underflow.
3. But `update_pool_rewards` then unconditionally sets `last_update_block = current_block_number()`, which is now `> E` because we are past expiry. This is exactly analogous to the Munchables bug where `plotMetadata[landlord].lastUpdated` becomes stale/misaligned relative to a bound that changed independently (`PRICE_PER_PLOT` there, `expiry_block` here) — except here it's the "current" cursor that outruns the fixed bound.
4. Any subsequent call to `stake`/`unstake`/`harvest_rewards` on this pool computes `rewardable_blocks_elapsed = last_block_reward_applicable(E).ensure_sub(last_update_block)`. Since `last_block_reward_applicable(E) == E` (we remain past expiry) and `last_update_block > E`, `ensure_sub` deterministically returns an error every single time, causing the whole extrinsic — including `unstake` — to revert.

Unlike the Solidity original where the underflow is a raw panic, here `ensure_sub` converts it into a propagated `DispatchError`, but the observable effect for the user is identical: the transaction always fails, and there is no unprivileged path to recover the staked tokens.

The only escape is for the pool's `admin` (a privileged `AdminOrigin`) to call `set_pool_expiry_block` to push `expiry_block` beyond the corrupted `last_update_block`: [4](#0-3) 

but this requires an out-of-band, privileged action; the freeze is triggered purely by permissionless, ordinary user interactions with an already-expired pool.

### Impact Explanation
Once any account interacts with an expired pool, `last_update_block` is pushed past `expiry_block`, after which every `unstake` and `harvest_rewards` call for *every* staker in that pool permanently reverts. This locks all staked tokens (a `fungible` asset held under a `Freeze`) with no permissionless recovery path, matching the "permanent user-fund lock" impact class. This is not an admin/governance-abuse scenario — the freeze is caused by normal expiry passing plus ordinary user calls, and only fixable by admin action after the fact.

### Likelihood Explanation
High. Pool expiry is a normal, expected lifecycle event (every pool is created with a finite `expiry_block`, enforced to be `> now` only at creation time — see `create_pool`'s check). Any staker or bystander calling `stake`, `unstake`, or `harvest_rewards` on a pool after its natural expiry will trigger the corrupting update; no attacker collusion, privileged role, or unusual configuration is required — it happens by default unless the admin proactively extends expiry before/at the exact expiry block on every touch.

### Recommendation
In `update_pool_rewards`, cap `last_update_block` at `min(current_block_number(), pool_info.expiry_block)` rather than always setting it to `current_block_number()`. This keeps the stored cursor consistent with the bound used in `reward_per_token`, preventing the cursor from ever exceeding `expiry_block` and eliminating the deterministic `ensure_sub` failure.

### Proof of Concept
1. Create a pool with `expiry_block = 100` and stake tokens before block 100 (reward_rate > 0).
2. Let the chain progress past block 100 (pool expired), and call `unstake` (or `harvest_rewards`/`stake`) once. Internally: `reward_per_token` succeeds (`last_update_block <= 100`), but `update_pool_rewards` sets `last_update_block = current_block_number()` (say block 150).
3. Call `unstake` again (or from another staker) at any block `>= 100`: `last_block_reward_applicable(100) = 100`, then `100.ensure_sub(150)` underflows and returns `Err`, causing the extrinsic to revert.
4. Every future `stake`/`unstake`/`harvest_rewards` call on this pool will revert identically until the admin calls `set_pool_expiry_block` to push `expiry_block` past 150 — until then, all staked funds in the pool are frozen and unwithdrawable by their owners.

### Citations

**File:** substrate/frame/asset-rewards/src/lib.rs (L656-665)
```rust
		#[pallet::call_index(6)]
		pub fn set_pool_expiry_block(
			origin: OriginFor<T>,
			pool_id: PoolId,
			new_expiry: DispatchTime<BlockNumberFor<T>>,
		) -> DispatchResult {
			let caller = T::CreatePoolOrigin::ensure_origin(origin.clone())
				.or_else(|_| ensure_signed(origin))?;
			<Self as RewardsPool<_>>::set_pool_expiry_block(&caller, pool_id, new_expiry)
		}
```

**File:** substrate/frame/asset-rewards/src/lib.rs (L754-784)
```rust
		pub fn update_pool_and_staker_rewards(
			pool_info: &PoolInfoFor<T>,
			staker_info: &PoolStakerInfo<T::Balance>,
		) -> Result<(PoolInfoFor<T>, PoolStakerInfo<T::Balance>), DispatchError> {
			let reward_per_token = Self::reward_per_token(&pool_info)?;
			let pool_info = Self::update_pool_rewards(pool_info, reward_per_token)?;

			let mut new_staker_info = staker_info.clone();
			new_staker_info.rewards = Self::derive_rewards(&staker_info, &reward_per_token)?;
			new_staker_info.reward_per_token_paid = pool_info.reward_per_token_stored;
			return Ok((pool_info, new_staker_info));
		}

		/// Computes update pool reward state.
		///
		/// Should be called every time the pool is adjusted, and a staker is not involved.
		///
		/// Returns the updated pool and staker info.
		///
		/// NOTE: this function has no side-effects. Side-effects such as storage modifications are
		/// the responsibility of the caller.
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

**File:** substrate/frame/asset-rewards/src/lib.rs (L794-801)
```rust
			let rewardable_blocks_elapsed: u32 =
				match Self::last_block_reward_applicable(pool_info.expiry_block)
					.ensure_sub(pool_info.last_update_block)?
					.try_into()
				{
					Ok(b) => b,
					Err(_) => return Err(Error::<T>::BlockNumberConversionError.into()),
				};
```

**File:** substrate/frame/asset-rewards/src/lib.rs (L826-833)
```rust
		fn last_block_reward_applicable(pool_expiry_block: BlockNumberFor<T>) -> BlockNumberFor<T> {
			let now = T::BlockNumberProvider::current_block_number();
			if now < pool_expiry_block {
				now
			} else {
				pool_expiry_block
			}
		}
```
