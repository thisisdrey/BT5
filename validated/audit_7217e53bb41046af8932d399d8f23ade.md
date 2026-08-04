## Title
Third-party reward-token deposits are permanently seizable by the pool admin in zero-activity `pallet-asset-rewards` pools - (File: `substrate/frame/asset-rewards/src/lib.rs`)

### Summary
`pallet-asset-rewards` allows **any signed account** (not just the pool admin) to top up a reward pool's balance via `deposit_reward_tokens`. If the pool never accrues staking activity (`total_tokens_staked == 0`), none of that deposited balance is ever streamed to stakers, and the entire pot — including the unrelated depositor's contribution — is later swept in full to the pool's `admin` via `cleanup_pool`. This mirrors the Debita `incentivizePair` bug class: tokens deposited for a period/pool with zero activity become unrecoverable by their original depositor, except here they are not merely locked — they are captured by a different beneficiary (the admin), with no accounting distinction between admin-funded reward budget and third-party donations.

### Finding Description
`deposit_reward_tokens` has no origin restriction — it is callable by anyone: [1](#0-0) 

Reward accrual is computed by `reward_per_token`, which short-circuits and does not consume any pool balance while `total_tokens_staked` is zero: [2](#0-1) 

This means any tokens sent to the pool account via `deposit_reward_tokens` while the pool has no active stakers (the "zero-activity epoch" analog) never get attributed to any staker's `reward_per_token_paid`/`rewards` accounting — they simply sit as raw balance on `pool_info.account`.

`cleanup_pool` is gated only on `stakers.is_none()` (i.e., no current `PoolStakers` entries) and transfers the **entire reducible balance** of the pool account to the `admin`, with no bookkeeping of who contributed what: [3](#0-2) 

Because `PoolInfo` and storage never separately track "admin-provided reward budget" versus "externally deposited top-ups," there is no way to distinguish or refund a depositor who is not the admin. The invariant that should hold — value contributed for future reward distribution is either distributed to stakers or returned to its contributor — is broken: it is unconditionally redirected to the `admin` field of `PoolInfo`, regardless of source.

### Impact Explanation
Any unprivileged user who calls `deposit_reward_tokens` to top up incentives for a pool loses those funds permanently to the pool's admin if the pool sees zero staking activity (e.g., pool created but never staked into, or all stakers exit before the depositor's contribution is consumed). This is a genuine "wrong beneficiary" / fund-loss bug affecting ordinary users interacting with a public extrinsic, not a privileged-admin-abuse scenario — the loss occurs purely as a side effect of the pallet's missing accounting, triggered by the admin performing an otherwise normal, intended action (`cleanup_pool`).

### Likelihood Explanation
`deposit_reward_tokens` is explicitly documented as a general convenience call ("tokens could be transferred directly to the pool pot address ... provided for convenience") implying it is expected to be used by parties other than just the admin. Any pool with low or intermittent activity (new pools before the first staker joins, or pools where all stakers eventually unstake) can trigger this path without any special conditions, attacker collusion, or governance involvement.

### Recommendation
Track deposited reward-asset balance separately from the amount actually required to fund the `reward_rate_per_block` schedule, and either: (1) restrict `deposit_reward_tokens` to the pool admin only, or (2) record depositor-attributable contributions and refund unconsumed amounts to their original depositors (not solely the admin) on `cleanup_pool`, mirroring the mitigation suggested for the Debita report (add a recovery path for contributors whose funds were never consumed by any activity).

### Proof of Concept
1. Admin calls `create_pool` with `staked_asset_id`, `reward_asset_id`, and some `reward_rate_per_block`.
2. A third-party user (not the admin) calls `deposit_reward_tokens(pool_id, amount)`, transferring `amount` of `reward_asset_id` into `pool_info.account`.
3. No account ever calls `stake` for this pool (`total_tokens_staked` stays `0`), so `reward_per_token` never advances and none of the deposited balance is earned by any staker.
4. Admin calls `cleanup_pool(pool_id)`; since `PoolStakers::iter_key_prefix(pool_id).next()` is `None`, the check passes, and the pallet transfers the pool account's full reducible balance — including the third party's deposit — to `pool_info.admin`.
5. The original depositor has no call or code path to reclaim their contribution.

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

**File:** substrate/frame/asset-rewards/src/lib.rs (L696-729)
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

			if let Some((who, cost)) = PoolCost::<T>::take(pool_id) {
				T::Consideration::drop(cost, &who)?;
			}

			Pools::<T>::remove(pool_id);

			Self::deposit_event(Event::PoolCleanedUp { pool_id });

			Ok(())
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
