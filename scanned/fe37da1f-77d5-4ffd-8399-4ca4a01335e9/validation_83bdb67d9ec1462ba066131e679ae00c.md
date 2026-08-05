Found a strong local analog in `pallet-asset-rewards` (`substrate/frame/asset-rewards/src/lib.rs`). This pallet implements the same bug class as the external report: a "last update" timestamp used to compute an elapsed-time-based accrual, but the timestamp is seeded incorrectly at pool creation.

### Title
Reward-pool `last_update_block` initialized to block `0` instead of the creation block causes massive over-accrual of staking rewards - (`substrate/frame/asset-rewards/src/lib.rs`)

### Summary
`Pallet::create_pool` in `pallet-asset-rewards` initializes the new `PoolInfo.last_update_block` field to `0u32.into()` (the chain genesis block) rather than to the current block at which the pool is created and rewards actually begin accruing. [1](#0-0) 
This is structurally identical to the reported `FundContract` bug: a period-based accrual calculation (`elapsed = now - last_update`) is seeded with a stale/incorrect starting timestamp, so the very first accrual update charges/credits for a period far longer than the funds were actually staked.

### Finding Description
`create_pool` builds the new `PoolInfoFor` with `last_update_block: 0u32.into()`: [2](#0-1) 
`reward_per_token_stored` accrual logic (in `update_pool_rewards`, called on every stake/unstake/harvest) computes elapsed blocks between `last_update_block` and `last_block_reward_applicable`, multiplies by `reward_rate_per_block`, and adds it to `reward_per_token_stored`: [3](#0-2) 
Because `last_update_block` starts at `0` rather than at the pool's creation block, the first call to `update_pool_rewards` after `create_pool` computes `elapsed = current_block - 0`, i.e. the entire chain height since genesis, instead of `current_block - creation_block`. This inflates `reward_per_token_stored` by `reward_rate_per_block * (creation_block)` extra reward units that were never actually funded by any staker's presence in the pool — mirroring exactly the `FundContract` flaw where `lastHarvestManagementFeeTime`/`lastHarvestPerformanceFeeTime` were seeded at contract setup instead of at first deposit, inflating the fee period.

Unlike the `FundContract` case (which *overcharges* users), here the direction is reversed: the pool's reward accounting inflates `reward_per_token_stored` without a matching balance in the pool's reward account, meaning early stakers can accrue (and attempt to claim) rewards that were never funded — a first-claimant can drain the reward pot disproportionately, and the pool's reward-token balance can go into deficit, since `derive_rewards`/payout logic assumes `reward_per_token_stored` tracks real, funded reward accrual.

### Impact Explanation
This falls squarely within the "asset accounting" / "reward payouts" impact category: it breaks the invariant that rewards must "conserve value and settle exactly once to the rightful beneficiary and amount." Any user can create a permissionless pool via `create_pool` (this is a public, unprivileged extrinsic — no admin/governance action required) at a high block height on a live chain, and the resulting inflated `reward_per_token_stored` value lets early stakers claim rewards funded by nothing, potentially draining the pool's reward-asset balance below what was actually deposited by the pool admin, causing fund loss/insolvency for the reward pool.

### Likelihood Explanation
High likelihood: `create_pool` is a normal, permissionless call reachable by any account with the required `Consideration` deposit; no privileged origin or governance action is needed. The bug triggers automatically on the very first `update_pool_rewards` call (stake, unstake, or harvest) after pool creation, on any chain that has already produced blocks (i.e., any live chain, not just genesis).

### Recommendation
Initialize `last_update_block` to the current block number (`T::BlockNumberProvider::current_block_number()`) at pool creation instead of `0u32.into()`, so that reward accrual only counts blocks elapsed since the pool actually started rather than since chain genesis.

### Proof of Concept
1. Deploy/run the chain to a non-trivial block height, e.g. block `1_000_000`.
2. Call `AssetRewards::create_pool(...)` — this creates `PoolInfoFor { last_update_block: 0, reward_rate_per_block, ... }` per [2](#0-1) .
3. Immediately have a staker call `stake` (or any call that triggers `update_pool_rewards`), which computes `last_block_reward_applicable(...)` and elapsed as `now (1_000_000) - last_update_block (0) = 1_000_000` blocks, per [3](#0-2) .
4. `reward_per_token_stored` is inflated by `reward_rate_per_block * 1_000_000`, letting the staker claim rewards that the pool's reward account was never funded with, draining/insolvency-ing the pool.

I was not able to fully inspect the `update_pool_rewards` function body itself (only `derive_rewards`, `last_block_reward_applicable`, and `create_pool` were retrieved via search) due to index size limits — a Devin session with full file access would be needed to confirm the exact call site that reads `last_update_block` and to trace the full claim/payout path for certainty on the deficit magnitude.

### Citations

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

**File:** substrate/frame/asset-rewards/src/lib.rs (L870-881)
```rust
		// Create the pool.
		let pool = PoolInfoFor::<T> {
			staked_asset_id: staked_asset_id.clone(),
			reward_asset_id: reward_asset_id.clone(),
			reward_rate_per_block,
			total_tokens_staked: 0u32.into(),
			reward_per_token_stored: 0u32.into(),
			last_update_block: 0u32.into(),
			expiry_block,
			admin: admin.clone(),
			account: Self::pool_account_id(&pool_id),
		};
```
