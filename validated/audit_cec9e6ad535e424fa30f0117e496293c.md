PRECISION_SCALING_FACTOR is only `4096`, confirming this is a small fixed-point multiplier (unlike Synthetix's usual `1e18`), which makes rounding-to-zero far more likely at the `substrate/frame/asset-rewards` pallet's per-block granularity.

### Title
`reward_per_token` precision loss allows griefer to permanently zero out staking rewards in `pallet-asset-rewards` - (File: `substrate/frame/asset-rewards/src/lib.rs`)

### Summary
`pallet-asset-rewards` implements a Synthetix-style JIT reward accumulator (`reward_per_token`) that is committed to storage (`reward_per_token_stored` / `last_update_block`) on every call to `stake`, `unstake`, or `harvest_rewards`. Because the precision multiplier is only `PRECISION_SCALING_FACTOR = 4096` and the elapsed-block delta used in the calculation is whatever interval has passed since the last commit, an attacker can force the delta to always be the minimum (1 block) by repeatedly invoking a public, permissionless, cheap extrinsic. Each such call commits a rounded-down (frequently zero) `reward_per_token` increment and advances `last_update_block`, permanently discarding the fractional reward for that block window — this is the exact same "reset the accumulator before it can grow" primitive used against `BathBuddy.getReward`.

### Finding Description
`reward_per_token` computes:
```
reward_per_token_stored + reward_rate_per_block * blocks_elapsed * PRECISION_SCALING_FACTOR / total_tokens_staked
``` [1](#0-0) 

`update_pool_rewards` then stores this computed value and unconditionally advances `last_update_block` to the current block, regardless of whether the increment was zero: [2](#0-1) 

This function is invoked from three public, unprivileged extrinsics — `stake`, `unstake`, and `harvest_rewards` — all of which call `update_pool_and_staker_rewards` at the top of the dispatch, before any other logic: [3](#0-2) [4](#0-3) 

Because `PRECISION_SCALING_FACTOR` is a fixed `4096` rather than something like `1e18`, integer division `reward_rate_per_block * blocks_elapsed * 4096 / total_tokens_staked` rounds to `0` whenever `total_tokens_staked` exceeds `reward_rate_per_block * blocks_elapsed * 4096`. If an attacker (who only needs to be an existing staker, achievable with a minimal `stake` call) triggers `stake`/`harvest_rewards` every single block, `blocks_elapsed` is always forced to `1`, maximizing the chance of the increment rounding to `0` on every commit. Since `update_pool_rewards` advances `last_update_block` regardless, that block's true reward contribution is discarded forever — the next computation starts counting from the new `last_update_block`, not from when the last *non-zero* accrual happened. This is functionally identical to the `BathBuddy` bug: `rewardPerToken` deltas computed over artificially shortened windows round to zero and `lastUpdateTime`/`last_update_block` is still advanced, permanently erasing that reward slice for every staker in the pool (not just the attacker), including legitimate small stakers who never interact.

### Impact Explanation
This degrades reward payout state (a live-scope impact: "permanent user-fund ... lock" / payout state not settling correctly) for a public `RewardsPool` primitive shipped in `substrate/frame/asset-rewards`. Any staked pool with a modest `reward_rate_per_block` relative to `total_tokens_staked` can have its rewards silently, permanently reduced or entirely zeroed by a low-cost, unprivileged attacker who only needs to be (or become, via a minimal `stake`) an existing pool participant and then repeatedly submit cheap self-targeted extrinsics every block. No privileged role, governance, relayer, or validator collusion is required — purely a public entry-point (`stake`/`harvest_rewards`) griefing vector.

### Likelihood Explanation
Likelihood depends on runtime configuration: pools with large `total_tokens_staked` relative to `reward_rate_per_block * PRECISION_SCALING_FACTOR (4096)` are vulnerable, and the attacker only pays ordinary transaction fees to call `stake`/`harvest_rewards` once per block — cheap on any parachain, and especially attractive if `asset-rewards` is deployed on high-throughput/low-fee chains (analogous to the original L2 cost argument). Given `PRECISION_SCALING_FACTOR` is only 4096 (12 bits) rather than a large fixed-point base, rounding-to-zero at 1-block granularity is a realistic, not merely theoretical, occurrence for realistically-sized pools.

### Recommendation
- Do not silently discard the elapsed-block window when the computed increment rounds to zero: either accumulate a remainder for the next computation, or use a much larger precision multiplier (e.g., `u128`-scale, matching `nomination-pools`' `FixedU128`-based `RewardCounter`, which is explicitly designed with accuracy notes for this exact class of issue) instead of `u16::4096`.
- Consider rate-limiting or requiring `update_pool_and_staker_rewards` to only commit `last_update_block` forward when the accrued increment is non-zero, or accumulate fractional remainders separately so repeated per-block calls cannot cause cumulative reward loss.

### Proof of Concept
1. `create_pool` with `reward_rate_per_block = R` and stake enough tokens from a "whale" account so `total_tokens_staked = S` satisfies `R * 4096 / S < 1` when computed over `blocks_elapsed = 1` (but `R * 4096 * N / S >= 1` for `N` blocks accumulated).
2. Attacker calls `stake(pool_id, 1)` (or `harvest_rewards`) every single block.
3. Each call runs `reward_per_token` with `blocks_elapsed = 1`, yielding an increment of `0` due to integer division, and `update_pool_rewards` advances `last_update_block` to the current block regardless [1](#0-0) .
4. After `N` blocks, a legitimate small staker who never interacted calls `harvest_rewards`: because `last_update_block` was ratcheted forward every block by the attacker, the pool's `reward_per_token_stored` never accumulated the true `R * N * 4096 / S` value that it would have if computed once over the full `N`-block window — the staker's `earned`/`derive_rewards` output is `0` or far below the honest expectation, replicating the `if_small_member_waits_long_enough_they_will_earn_rewards`-style expectation but broken by attacker-forced per-block commits (contrast with the "waits long enough" test path, which relies on the reward window being computed over a sufficiently large delta to avoid rounding to zero) [5](#0-4) .

### Citations

**File:** substrate/frame/asset-rewards/src/lib.rs (L472-480)
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

**File:** substrate/frame/asset-rewards/src/lib.rs (L569-585)
```rust
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

**File:** substrate/frame/asset-rewards/src/lib.rs (L794-809)
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
```

**File:** substrate/frame/nomination-pools/src/tests.rs (L5957-6024)
```rust
	#[test]
	fn if_small_member_waits_long_enough_they_will_earn_rewards() {
		// create a pool that has a quarter of the current polkadot issuance
		ExtBuilder::default()
			.ed(DOT)
			.min_bond(POLKADOT_TOTAL_ISSUANCE_GENESIS / 4)
			.build_and_execute(|| {
				assert_eq!(
					pool_events_since_last_call(),
					vec![
						Event::Created { depositor: 10, pool_id: 1 },
						Event::Bonded {
							member: 10,
							pool_id: 1,
							bonded: 2500000000000000000,
							joined: true,
						},
						Event::MetadataUpdated { pool_id: 1, caller: 900 },
					]
				);

				// and have a tiny fish join the pool as well..
				Currency::set_balance(&20, 20 * DOT);
				assert_ok!(Pools::join(RuntimeOrigin::signed(20), 10 * DOT, 1));

				// earn some small rewards
				deposit_rewards(DOT / 1000);

				// no point in claiming for 20 (nonetheless, it should be harmless)
				assert!(pending_rewards(20).unwrap().is_zero());
				assert_ok!(Pools::claim_payout(RuntimeOrigin::signed(10)));
				assert_eq!(
					pool_events_since_last_call(),
					vec![
						Event::Bonded {
							member: 20,
							pool_id: 1,
							bonded: 100000000000,
							joined: true
						},
						Event::PaidOut { member: 10, pool_id: 1, payout: 9999997 }
					]
				);

				// earn some small more, still nothing can be claimed for 20, but 10 claims their
				// share.
				deposit_rewards(DOT / 1000);
				assert!(pending_rewards(20).unwrap().is_zero());
				assert_ok!(Pools::claim_payout(RuntimeOrigin::signed(10)));
				assert_eq!(
					pool_events_since_last_call(),
					vec![Event::PaidOut { member: 10, pool_id: 1, payout: 10000000 }]
				);

				// earn some more rewards, this time 20 can also claim.
				deposit_rewards(DOT / 1000);
				assert_eq!(pending_rewards(20).unwrap(), 1);
				assert_ok!(Pools::claim_payout(RuntimeOrigin::signed(10)));
				assert_ok!(Pools::claim_payout(RuntimeOrigin::signed(20)));
				assert_eq!(
					pool_events_since_last_call(),
					vec![
						Event::PaidOut { member: 10, pool_id: 1, payout: 10000000 },
						Event::PaidOut { member: 20, pool_id: 1, payout: 1 }
					]
				);
			});
	}
```
