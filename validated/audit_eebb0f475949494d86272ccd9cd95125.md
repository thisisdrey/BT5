## Analog Identified: Reward-per-token precision loss in `pallet-asset-rewards` [1](#0-0) 

### Title
Reward-per-token accumulator rounds down to zero for pools with large `total_tokens_staked`, permanently freezing staker rewards - (File: `substrate/frame/asset-rewards/src/lib.rs`)

### Summary
`pallet-asset-rewards` computes a Uniswap/MasterChef-style reward-per-token accumulator using a fixed precision multiplier `PRECISION_SCALING_FACTOR = 4096` (2^12). This is orders of magnitude smaller than the precision factors typically used for this pattern (e.g. 1e12/1e18/1e36 in the Biconomy report). Because `total_tokens_staked` is an unbounded, permissionlessly-controlled `Balance` (any signed account can create a pool and any signed account can stake arbitrary amounts of an arbitrary fungible asset), the division `reward_rate_per_block * blocks_elapsed * 4096 / total_tokens_staked` silently truncates to `0` whenever the staked total is large relative to the reward rate — exactly the H-05 pattern where `ACC_TOKEN_PRECISION`/`totalSharesStaked` imbalance zeroes the accumulator.

### Finding Description
`reward_per_token` computes the incremental reward-per-token delta as: [2](#0-1) 

```rust
Ok(pool_info.reward_per_token_stored.ensure_add(
    pool_info
        .reward_rate_per_block
        .ensure_mul(rewardable_blocks_elapsed.into())?
        .ensure_mul(PRECISION_SCALING_FACTOR.into())?
        .ensure_div(pool_info.total_tokens_staked)?,
)?)
```

`PRECISION_SCALING_FACTOR` is a compile-time constant `4096` (`u16`), used both here and in `derive_rewards`: [3](#0-2) 

`EnsureDiv::ensure_div` only errors on division-by-zero or genuine overflow — ordinary integer truncation to `0` is not an error condition, so this behaves identically to the vulnerable Solidity `/` operator in the Biconomy report. `total_tokens_staked` accumulates from the permissionless `stake` extrinsic with no upper bound check: [4](#0-3) 

Pool creation itself is permissionless in at least two shipped runtime configurations (`asset-hub-rococo` and the reference `node/runtime`), where `CreatePoolOrigin = EnsureSigned<AccountId>`: [5](#0-4) [6](#0-5) 

Any unprivileged user can therefore create a pool with any existing asset pair, and any unprivileged staker can stake it with a large quantity of a high-decimal asset, driving `total_tokens_staked` up until `reward_rate_per_block * blocks_elapsed * 4096` no longer exceeds `total_tokens_staked`. From that point on `reward_per_token` never advances (delta = 0 every update), so `derive_rewards` computes zero new rewards for every staker regardless of actual pool funding — the reward asset held in `pool_info.account` becomes stuck and unclaimable by any legitimate staker.

### Impact Explanation
This directly breaks the pallet's core value-conservation invariant ("stakers accrue rewards proportional to their stake") required by the Polkadot SDK Pivots. Reward tokens held in the pool's dedicated account cannot be claimed by stakers because the accrual accumulator is mathematically dead at realistic (asset-with-many-decimals) stake sizes — this is a real fund lock, not merely dust loss, since a single large staker permanently zeroes rewards for every staker in the pool, including honest ones who staked before the precision collapse. Given `PRECISION_SCALING_FACTOR` is only `4096` versus the `1e18`-class scaling used elsewhere in the codebase (e.g. nomination-pools' `FixedU128` reward counter, `substrate/frame/nomination-pools/src/lib.rs:1506-1509`), the safety margin here is drastically smaller, making the zero-rounding threshold trivially reachable with common 18-decimal assets.

### Likelihood Explanation
No privileged actor, governance action, malicious validator/collator, or leaked key is required. Any signed account can create a pool (permissionless `CreatePoolOrigin` in shipped runtimes) and any signed account can stake an arbitrary amount of any listed fungible asset via the public `stake` extrinsic. Reaching the truncation threshold only requires ordinary token economics (a token with 18 decimals and a total supply/stake in the billions easily exceeds `reward_rate_per_block * blocks * 4096`), making this reachable in normal operation, not just adversarial griefing.

### Recommendation
Increase `PRECISION_SCALING_FACTOR` to a much larger fixed-point base (e.g. `1_000_000_000_000_000_000` as `u128`, matching `FixedU128`-style scaling elsewhere in the codebase), and/or perform the reward-per-token computation in a wider intermediate type (e.g. `U256`) before dividing, similar to `point_to_balance` in nomination-pools (`substrate/frame/nomination-pools/src/lib.rs:3503-3522`), which uses `T::BalanceToU256`/`T::U256ToBalance` to avoid truncation. Additionally consider bounding `total_tokens_staked` relative to `reward_rate_per_block` or emitting a defensive error/event when the computed delta is zero despite a non-zero elapsed reward budget, so the condition is detected rather than silently dropped.

### Proof of Concept
1. `create_pool` with `staked_asset_id` = an 18-decimal asset, `reward_rate_per_block` = `1_000_000_000_000` (1e12), any `reward_asset_id`.
2. Staker A calls `stake(pool_id, 1_000_000 * 10^18)` (1,000,000 tokens of an 18-decimal asset) — `total_tokens_staked` becomes `1e24`.
3. After `14_400` blocks (~1 day at 6s blocks), `reward_per_token` computes:
   `numerator = 1e12 * 14_400 * 4096 ≈ 5.9e19`
   `5.9e19 / 1e24 = 0` (integer division truncates to zero).
4. Staker A calls the harvest/claim path; `derive_rewards` yields `0` new rewards despite the pool having accrued `reward_rate_per_block * 14_400` worth of reward tokens in `pool_info.account`.
5. The reward tokens remain stuck in the pool account, unclaimable by any staker as long as `total_tokens_staked` stays large relative to `reward_rate_per_block`.

### Citations

**File:** substrate/frame/asset-rewards/src/lib.rs (L117-118)
```rust
/// Multiplier to maintain precision when calculating rewards.
pub(crate) const PRECISION_SCALING_FACTOR: u16 = 4096;
```

**File:** substrate/frame/asset-rewards/src/lib.rs (L469-502)
```rust
		/// Stake additional tokens in a pool.
		///
		/// A freeze is placed on the staked tokens.
		#[pallet::call_index(1)]
		pub fn stake(origin: OriginFor<T>, pool_id: PoolId, amount: T::Balance) -> DispatchResult {
			let staker = ensure_signed(origin)?;

			// Always start by updating staker and pool rewards.
			let pool_info = Pools::<T>::get(pool_id).ok_or(Error::<T>::NonExistentPool)?;
			let staker_info = PoolStakers::<T>::get(pool_id, &staker).unwrap_or_default();
			let (mut pool_info, mut staker_info) =
				Self::update_pool_and_staker_rewards(&pool_info, &staker_info)?;

			T::AssetsFreezer::increase_frozen(
				pool_info.staked_asset_id.clone(),
				&FreezeReason::Staked.into(),
				&staker,
				amount,
			)?;

			// Update Pools.
			pool_info.total_tokens_staked.ensure_add_assign(amount)?;

			Pools::<T>::insert(pool_id, pool_info);

			// Update PoolStakers.
			staker_info.amount.ensure_add_assign(amount)?;
			PoolStakers::<T>::insert(pool_id, &staker, staker_info);

			// Emit event.
			Self::deposit_event(Event::Staked { staker, pool_id, amount });

			Ok(())
		}
```

**File:** substrate/frame/asset-rewards/src/lib.rs (L786-810)
```rust
		/// Derives the current reward per token for this pool.
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

**File:** substrate/frame/asset-rewards/src/lib.rs (L812-824)
```rust
		/// Derives the amount of rewards earned by a staker.
		///
		/// This is a helper function for `update_pool_rewards` and should not be called directly.
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

**File:** cumulus/parachains/runtimes/assets/asset-hub-rococo/src/lib.rs (L1070-1089)
```rust
impl pallet_asset_rewards::Config for Runtime {
	type RuntimeEvent = RuntimeEvent;
	type PalletId = AssetRewardsPalletId;
	type Balance = Balance;
	type Assets = NativeAndAllAssets;
	type AssetsFreezer = NativeAndAllAssetsFreezer;
	type AssetId = xcm::v5::Location;
	type CreatePoolOrigin = EnsureSigned<AccountId>;
	type RuntimeFreezeReason = RuntimeFreezeReason;
	type Consideration = HoldConsideration<
		AccountId,
		Balances,
		RewardsPoolCreationHoldReason,
		ConstantStoragePrice<StakePoolCreationDeposit, Balance>,
	>;
	type WeightInfo = weights::pallet_asset_rewards::WeightInfo<Runtime>;
	type BlockNumberProvider = frame_system::Pallet<Runtime>;
	#[cfg(feature = "runtime-benchmarks")]
	type BenchmarkHelper = PalletAssetRewardsBenchmarkHelper;
}
```

**File:** substrate/bin/node/runtime/src/lib.rs (L2096-2115)
```rust
impl pallet_asset_rewards::Config for Runtime {
	type RuntimeEvent = RuntimeEvent;
	type RuntimeFreezeReason = RuntimeFreezeReason;
	type AssetId = NativeOrWithId<u32>;
	type Balance = Balance;
	type Assets = NativeAndAssets;
	type PalletId = StakingRewardsPalletId;
	type CreatePoolOrigin = EnsureSigned<AccountId>;
	type WeightInfo = ();
	type AssetsFreezer = NativeAndAssetsFreezer;
	type Consideration = HoldConsideration<
		AccountId,
		Balances,
		CreationHoldReason,
		ConstantStoragePrice<StakePoolCreationDeposit, Balance>,
	>;
	type BlockNumberProvider = frame_system::Pallet<Runtime>;
	#[cfg(feature = "runtime-benchmarks")]
	type BenchmarkHelper = AssetRewardsBenchmarkHelper;
}
```
