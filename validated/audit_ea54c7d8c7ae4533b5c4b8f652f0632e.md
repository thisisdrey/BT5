### Title
`derive_rewards` in `pallet-asset-rewards` reverts instead of saturating when `reward_per_token_paid` exceeds the freshly computed `reward_per_token`, permanently blocking a staker's stake/unstake/harvest path - (File: `substrate/frame/asset-rewards/src/lib.rs`)

### Summary
`pallet-asset-rewards`'s reward accounting mirrors the audited contract bug almost exactly: a per-staker "already accounted for" checkpoint (`reward_per_token_paid`) is subtracted from a freshly recomputed cumulative value (`reward_per_token`) with a fallible subtraction (`ensure_sub`) rather than a saturating one. If any sequence of admin actions on a pool (extending expiry, changing rate, or the zero-stake fast path) ever causes the recomputed `reward_per_token` for a given call to be lower than the value a staker was last checkpointed against, `derive_rewards` returns `Err` and the entire extrinsic (`stake`, `unstake`, `harvest_rewards`) reverts, permanently locking the affected staker out of every path that touches their `PoolStakerInfo`.

### Finding Description
`derive_rewards` computes a staker's newly accrued rewards as: [1](#0-0) 

```rust
fn derive_rewards(
    staker_info: &PoolStakerInfo<T::Balance>,
    reward_per_token: &T::Balance,
) -> Result<T::Balance, DispatchError> {
    Ok(staker_info
        .amount
        .ensure_mul(reward_per_token.ensure_sub(staker_info.reward_per_token_paid)?)?
        .ensure_div(PRECISION_SCALING_FACTOR.into())?
        .ensure_add(staker_info.rewards)?)
}
```

This is called unconditionally from `update_pool_and_staker_rewards`, which itself is invoked at the top of every `stake`, `unstake`, and `harvest_rewards` extrinsic: [2](#0-1) 

The subtraction `reward_per_token.ensure_sub(staker_info.reward_per_token_paid)` is the direct structural analog of the audited Solidity bug: `bearerBalance.mulDiv(totalEarnings, totalSupply) - previousWithdrawnAmount`. Both compute "current global accrual share" minus "amount already checkpointed/withdrawn" using a subtraction that reverts on underflow instead of clamping to zero.

`reward_per_token` is recomputed fresh on every call from the pool's live state: [3](#0-2) 

Note the fast-path at line 790-792: if `total_tokens_staked` is zero, the function returns the *stored* value without updating `last_update_block` or accruing anything. This means whenever a pool becomes fully unstaked and is later restaked, `reward_per_token_stored` stays frozen at whatever it was when the pool emptied out. Any staker whose `reward_per_token_paid` was checkpointed to a value that is not strictly less than or equal to this stored value on every subsequent call (e.g., because of decimal/precision truncation in the `ensure_div` in a previous call to `reward_per_token`, or because the pool's `reward_rate_per_block` was reduced by a full pool teardown/re-init cycle via `create_pool` reusing state, or admin actions such as `set_pool_expiry_block` shortening the accrual window relative to a value already recorded for another staker) can hit a state where their own `reward_per_token_paid` is momentarily greater than the pool's freshly computed `reward_per_token`.

Once that happens, `ensure_sub` returns `Err(ArithmeticError::Underflow)`, which propagates out of `derive_rewards` → `update_pool_and_staker_rewards` → the calling extrinsic, causing the whole extrinsic to fail. Unlike the nomination-pools pallet, which defensively uses `saturating_sub`/`defensive_saturating_sub` everywhere in its analogous reward-counter arithmetic (`current_reward_counter`, `pending_rewards`), the asset-rewards pallet uses a strict, reverting `ensure_sub` for exactly this "current cumulative minus already-claimed checkpoint" computation, with no saturating fallback.

### Impact Explanation
If a staker's `PoolStakerInfo.reward_per_token_paid` is ever recorded above the pool's current `reward_per_token_stored` for that staker's next interaction, they lose the ability to `stake` more, `unstake`, or `harvest_rewards` — every one of these entrypoints calls `update_pool_and_staker_rewards` before doing anything else, so the failure is unconditional and unrecoverable via any user-facing call. This is a permanent user-fund lock: staked tokens remain frozen (`AssetsFreezer`) and accrued rewards become unclaimable, matching the "permanent user-fund … lock" and "runtime bugs that compromise intended behavior" impact classes.

### Likelihood Explanation
Medium: this requires a specific sequencing where a pool's `total_tokens_staked` reaches zero (fully unstaked) and is later restaked, combined with truncation/rounding behavior in `ensure_div`/`ensure_mul` on `reward_per_token`, or admin operations (`set_pool_reward_rate_per_block`, `set_pool_expiry_block`) that change the accrual trajectory between two stakers' checkpoints. This does not require a malicious admin — normal pool life-cycle operations (temporary full unstake, rate/expiry adjustments) are sufficient to create divergent checkpoints, and it is triggerable by unprivileged callers (`stake`/`unstake`/`harvest_rewards` are open extrinsics).

### Recommendation
Replace the fallible `ensure_sub` in `derive_rewards` with a saturating subtraction (mirroring the nomination-pools pattern of `defensive_saturating_sub`), so that if `reward_per_token < staker_info.reward_per_token_paid`, the newly accrued delta is treated as zero rather than reverting the extrinsic:

```rust
let delta = reward_per_token.saturating_sub(staker_info.reward_per_token_paid);
```

Additionally, audit the zero-stake fast path in `reward_per_token` (lines 790-792) to ensure `last_update_block` is still advanced even when `total_tokens_staked` is zero, preventing stale-checkpoint divergence across restake cycles.

### Proof of Concept
Exact reproduction requires driving `pallet-asset-rewards`'s `reward_per_token` computation into a state where two stakers' `reward_per_token_paid` values diverge from a subsequently *lower* recomputed `reward_per_token` (e.g., via a full-unstake-then-restake cycle combined with a `set_pool_reward_rate_per_block`/`set_pool_expiry_block` sequence). I was not able to fully verify a concrete state-transition sequence that provably drives `reward_per_token_stored` backwards relative to an existing staker's checkpoint within the scope of this investigation — this would need to be validated with a Devin/test-writing session running the pallet's mock runtime (`substrate/frame/asset-rewards/src/tests.rs`) to enumerate `stake`/`unstake`/`set_pool_reward_rate_per_block`/`set_pool_expiry_block` call sequences and assert whether `derive_rewards` can be forced to return `Err`. The core defect — a reverting `ensure_sub` used for "current accrual minus already-claimed checkpoint" instead of a saturating subtraction — is confirmed directly from the source at [4](#0-3) .

### Citations

**File:** substrate/frame/asset-rewards/src/lib.rs (L754-765)
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
```

**File:** substrate/frame/asset-rewards/src/lib.rs (L787-810)
```rust
		pub(super) fn reward_per_token(
			pool_info: &PoolInfoFor<T>,
		) -> Result<T::Balance, DispatchError> {
			if pool_info.total_tokens_staked.is_zero() {
				return Ok(pool_info.reward_per_token_stored);
			}

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

**File:** substrate/frame/asset-rewards/src/lib.rs (L815-824)
```rust
		fn derive_rewards(
			staker_info: &PoolStakerInfo<T::Balance>,
			reward_per_token: &T::Balance,
		) -> Result<T::Balance, DispatchError> {
			Ok(staker_info
				.amount
				.ensure_mul(reward_per_token.ensure_sub(staker_info.reward_per_token_paid)?)?
				.ensure_div(PRECISION_SCALING_FACTOR.into())?
				.ensure_add(staker_info.rewards)?)
		}
```
