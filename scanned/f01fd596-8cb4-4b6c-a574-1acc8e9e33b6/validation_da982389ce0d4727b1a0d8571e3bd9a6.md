## Analysis

The MultiRewards bug is a **floor-division precision-loss pattern**: computing a *rate* as `total / duration` (or, more generally, `numerator / denominator`) truncates a remainder that is never re-credited anywhere, so it silently and permanently leaves value stuck in the contract, growing with every invocation.

The closest local analog in `polkadot-sdk` is in `pallet-asset-rewards` (`substrate/frame/asset-rewards/src/lib.rs`), which implements a Synthetix-`StakingRewards`-style JIT reward accounting scheme — the same family of algorithm the reported contract belongs to.

### Title
Compounded floor-division in `reward_per_token`/`derive_rewards` permanently strands reward-asset dust in `pallet-asset-rewards` pool accounts - (File: `substrate/frame/asset-rewards/src/lib.rs`)

### Summary
`pallet-asset-rewards` computes staker rewards through two chained integer divisions, each of which truncates towards zero. The resulting rounding remainder is never tracked or re-added to a subsequent computation, so a portion of every reward accrual is permanently unaccounted for and can never be claimed by any staker, mirroring the exact "accumulating irrecoverable remainder" defect described in the MultiRewards report.

### Finding Description
`reward_per_token` computes the pool's cumulative reward-per-token using: [1](#0-0) 

```rust
Ok(pool_info.reward_per_token_stored.ensure_add(
    pool_info
        .reward_rate_per_block
        .ensure_mul(rewardable_blocks_elapsed.into())?
        .ensure_mul(PRECISION_SCALING_FACTOR.into())?
        .ensure_div(pool_info.total_tokens_staked)?,
)?)
```

This is a floor division by `total_tokens_staked`, scaled only by the small constant `PRECISION_SCALING_FACTOR = 4096`: [2](#0-1) 

The result is then used a second time with another floor division in `derive_rewards`: [3](#0-2) 

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

Both divisions (`.../ total_tokens_staked` and `.../ PRECISION_SCALING_FACTOR`) truncate. Because `PRECISION_SCALING_FACTOR` is only `4096` (12 bits of precision) rather than a much larger fixed-point scale (e.g. `10^18` as used elsewhere in FRAME, such as nomination pools' `FixedU128` reward counter — see `substrate/frame/nomination-pools/src/lib.rs:1506-1509`), the truncated remainder on each of these two divisions can be a non-negligible fraction of a block's reward, especially when `total_tokens_staked` is large relative to `reward_rate_per_block * PRECISION_SCALING_FACTOR`.

Unlike `pallet-nomination-pools`, which derives `current_payout_balance` directly from the pool account's *actual balance* each time (so any prior rounding dust is automatically re-swept into the next computation, see `substrate/frame/nomination-pools/src/lib.rs:1462-1465`), `pallet-asset-rewards` tracks rewards purely via the `reward_per_token_stored` rate accumulator. The truncated remainder from each block's `reward_per_token` update is discarded and never re-added to `reward_per_token_stored` or `staker_info.rewards` on the next call — there is no residual-tracking field analogous to what the report recommends. The pool account is funded out-of-band by the admin (see module docs: "Care should be taken by the pool operator to keep pool accounts adequately funded with the reward asset" at line 37), so the truncated fraction of each accrual is real reward-asset value that sits in the pool's dedicated account but that no staker's `reward_per_token`/`derive_rewards` computation can ever reconstruct or claim, because `reward_per_token_stored` has already permanently lost that fraction.

### Impact Explanation
This is a value-conservation defect in a live FRAME pallet: reward assets locked into a pool's dedicated account (`pool_account_id`) are silently and permanently under-distributed to stakers due to compounded floor division, growing on every block/every `stake`/`unstake`/`harvest_rewards` call that triggers `update_pool_and_staker_rewards`. Over the life of a long-running, heavily staked pool this can add up to a persistent, unrecoverable-by-stakers shortfall, i.e. reward value that never settles to its rightful beneficiaries — the exact "conserve value and settle exactly once to the rightful beneficiary and amount" invariant called out in the impact gate.

### Likelihood Explanation
High: this occurs automatically on every reward accrual for every pool with `total_tokens_staked` not evenly dividing `reward_rate_per_block * blocks_elapsed * PRECISION_SCALING_FACTOR`, and again on every `derive_rewards` call. No malicious actor, privileged action, or special conditions are required — it is triggered by ordinary `stake`/`unstake`/`harvest_rewards` calls, which are public, unprivileged extrinsics.

### Recommendation
Track the truncated remainder from each division (e.g., store a per-pool "dust" accumulator similarly to how `pallet-vesting`'s new `VestedPayout::vested_transfer` deliberately rounds `per_block` up rather than down to avoid under-distribution, see `substrate/frame/vesting/src/tests.rs:1339-1354`), and fold it back into the next `reward_per_token`/`derive_rewards` computation, or increase `PRECISION_SCALING_FACTOR` substantially (e.g. to a `FixedU128`-based accumulator as used in `pallet-nomination-pools`) so that the truncated fraction becomes economically negligible rather than compounding.

### Proof of Concept
1. Create a pool with `reward_rate_per_block = 100` and `PRECISION_SCALING_FACTOR = 4096` (fixed constant).
2. Stake an amount such that `100 * blocks_elapsed * 4096` is not evenly divisible by `total_tokens_staked` (e.g. `total_tokens_staked = 4097` after multiple stakers join across several blocks, as exercised in `staker_rewards_are_affected_correctly`-style tests: [4](#0-3)  and the multi-staker scenario at [5](#0-4) ).
3. Advance blocks and call `harvest_rewards` for all stakers repeatedly.
4. Sum all harvested amounts across all stakers and compare against `reward_rate_per_block * total_blocks_elapsed`; the sum will be strictly less due to the compounded floor divisions in `reward_per_token` and `derive_rewards`, and the shortfall is not recoverable by any staker (only recoverable by the pool admin via `cleanup_pool`, once all stakers have exited — see [6](#0-5) ).

### Citations

**File:** substrate/frame/asset-rewards/src/lib.rs (L117-118)
```rust
/// Multiplier to maintain precision when calculating rewards.
pub(crate) const PRECISION_SCALING_FACTOR: u16 = 4096;
```

**File:** substrate/frame/asset-rewards/src/lib.rs (L706-718)
```rust
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

**File:** substrate/frame/asset-rewards/src/lib.rs (L803-809)
```rust
			Ok(pool_info.reward_per_token_stored.ensure_add(
				pool_info
					.reward_rate_per_block
					.ensure_mul(rewardable_blocks_elapsed.into())?
					.ensure_mul(PRECISION_SCALING_FACTOR.into())?
					.ensure_div(pool_info.total_tokens_staked)?,
			)?)
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

**File:** substrate/frame/asset-rewards/src/tests.rs (L1063-1094)
```rust
	#[test]
	fn staker_rewards_are_affected_correctly() {
		new_test_ext().execute_with(|| {
			let admin = 1;
			let staker = 2;
			let pool_id = 0;
			let new_reward_rate = 150;
			create_default_pool();

			// Stake some tokens, and accumulate 10 blocks of rewards at the default pool rate (100)
			System::set_block_number(10);
			assert_ok!(StakingRewards::stake(RuntimeOrigin::signed(staker), pool_id, 1000));
			System::set_block_number(20);

			// Increase the reward rate
			assert_ok!(StakingRewards::set_pool_reward_rate_per_block(
				RuntimeOrigin::signed(admin),
				pool_id,
				new_reward_rate
			));

			// Accumulate 10 blocks of rewards at the new rate
			System::set_block_number(30);

			// Check that rewards are calculated correctly with the updated rate
			assert_hypothetically_earned(
				staker,
				10 * 100 + 10 * new_reward_rate,
				pool_id,
				NativeOrWithId::<u32>::Native,
			);
		});
```

**File:** substrate/frame/asset-rewards/src/tests.rs (L1325-1358)
```rust
		System::set_block_number(9);
		assert_ok!(StakingRewards::stake(RuntimeOrigin::signed(staker2), pool_id, 100));
		// At this point
		// - Staker 1 has earned 200 (100*2) tokens.
		// - Staker 2 has earned 0 tokens.
		// - Staker 1 is earning 50 tokens per block.
		// - Staker 2 is earning 50 tokens per block.

		// Check that Staker 1 has earned 200 tokens and Staker 2 has earned 0 tokens.
		assert_hypothetically_earned(staker1, 200, pool_id, reward_asset_id.clone());
		assert_hypothetically_earned(staker2, 0, pool_id, reward_asset_id.clone());

		// Block 12: Staker 1 stakes an additional 100 tokens.
		System::set_block_number(12);
		assert_ok!(StakingRewards::stake(RuntimeOrigin::signed(staker1), pool_id, 100));
		// At this point
		// - Staker 1 has earned 350 (200 + (50 * 3)) tokens.
		// - Staker 2 has earned 150 (50 * 3) tokens.
		// - Staker 1 is earning 66.66 tokens per block.
		// - Staker 2 is earning 33.33 tokens per block.

		// Check that Staker 1 has earned 350 tokens and Staker 2 has earned 150 tokens.
		assert_hypothetically_earned(staker1, 350, pool_id, reward_asset_id.clone());
		assert_hypothetically_earned(staker2, 150, pool_id, reward_asset_id.clone());

		// Block 22: Staker 1 unstakes 100 tokens.
		System::set_block_number(22);
		assert_ok!(StakingRewards::unstake(RuntimeOrigin::signed(staker1), pool_id, 100, None));
		// - Staker 1 has earned 1016 (350 + 66.66 * 10) tokens.
		// - Staker 2 has earned 483 (150 + 33.33 * 10) tokens.
		// - Staker 1 is earning 50 tokens per block.
		// - Staker 2 is earning 50 tokens per block.
		assert_hypothetically_earned(staker1, 1016, pool_id, reward_asset_id.clone());
		assert_hypothetically_earned(staker2, 483, pool_id, reward_asset_id.clone());
```
