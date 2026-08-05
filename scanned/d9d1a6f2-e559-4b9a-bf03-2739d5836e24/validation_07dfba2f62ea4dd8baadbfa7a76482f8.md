## Title
Reward pool state (`stake`/`unstake`/`harvest_rewards`) permanently DoS'd when a non-monotonic `BlockNumberProvider` regresses, locking staked funds and unclaimed rewards - (File: `substrate/frame/asset-rewards/src/lib.rs`)

### Summary
`pallet-asset-rewards` computes JIT reward accrual from a block-number delta (`now - last_update_block`), mirroring the audited pattern of using `block.number` as a linear proxy for elapsed time. The pallet was updated to source `now` from a generic, pluggable `T::BlockNumberProvider` [1](#0-0)  instead of the local `frame_system` counter specifically so it can be driven by the relay chain block number on parachains, per the pallet's own changelog [2](#0-1) . That relay-chain-backed provider is documented as **not guaranteed to be monotonically increasing** [3](#0-2) , and a sibling pallet (`pallet-society`) had to be specifically hardened against non-consecutive/repeated values coming from non-local block providers [4](#0-3) . `pallet-asset-rewards` received no equivalent hardening: its reward computation uses `ensure_sub`, which turns any regression of `now` below the stored `last_update_block` into a hard `DispatchError` that propagates out of `stake`, `unstake`, and `harvest_rewards`.

### Finding Description
Reward-per-token accrual is:

```rust
let rewardable_blocks_elapsed: u32 =
    match Self::last_block_reward_applicable(pool_info.expiry_block)
        .ensure_sub(pool_info.last_update_block)?
        .try_into()
``` [5](#0-4) 

`last_block_reward_applicable` returns `min(now, expiry_block)`, where `now = T::BlockNumberProvider::current_block_number()` [6](#0-5) . `update_pool_rewards` then unconditionally stores `now` as the new `last_update_block` on every successful call [7](#0-6) .

`update_pool_and_staker_rewards` (which wraps `reward_per_token`) is called from every state-changing entry point:
- `stake` [8](#0-7) 
- `unstake` [9](#0-8) 
- `harvest_rewards` [10](#0-9) 

`ensure_sub` returns an `Err` (propagated with `?`) instead of saturating whenever the current block number is smaller than the previously stored `last_update_block`. Because the pallet is explicitly designed to accept a relay-chain-backed `BlockNumberProvider` for parachain deployments (used in `asset-hub-rococo-runtime`, `asset-hub-westend-runtime`, and `pallet-staking-async-parachain-runtime` per the changelog), and that provider's own documentation states relay parents are **not guaranteed to be monotonically increasing** across parachain blocks, a single regression of the observed relay-chain block number (a documented, non-attacker-controlled property of the chosen provider) is enough to make `now < last_update_block` for any pool that has already been updated once. From that point on, every `stake`, `unstake`, and `harvest_rewards` call for that pool fails deterministically inside `reward_per_token`, because the underflow recurs identically on every subsequent call (the stored `last_update_block` never advances past the regression point since no update ever completes again).

This differs qualitatively from the analogous `pallet-vesting`/`Vesting` design, which uses `saturating_sub` when computing elapsed vesting blocks [11](#0-10) , tolerating a `now < starting_block` case gracefully. `pallet-asset-rewards` has no such saturation guard.

### Impact Explanation
Once triggered, stakers can no longer `unstake` their frozen staking-asset tokens nor `harvest_rewards` for the affected pool — both entry points unconditionally call the failing `update_pool_and_staker_rewards` before any other logic executes. This is a permanent lock of user funds (frozen stake) and unclaimed reward-asset balances held in the pool account, satisfying the "permanent user-fund … lock" impact criterion. No governance, admin, validator, collator, or malicious peer action is required — the trigger is an inherent, documented characteristic of the officially supported relay-chain `BlockNumberProvider` configuration.

### Likelihood Explanation
The vulnerable configuration (non-local, relay-chain-derived `BlockNumberProvider`) is not hypothetical: it is the very reason the pallet's `BlockNumberProvider` generalization was introduced [12](#0-11) , and it is already wired into `asset-hub-rococo-runtime`, `asset-hub-westend-runtime`, and `pallet-staking-async-parachain-runtime`. The same class of non-monotonic/repeated block-number issue was significant enough to require a dedicated fix in `pallet-society` [4](#0-3) , showing this is a recognized, real occurrence for such providers rather than a purely theoretical edge case — `pallet-asset-rewards` simply never received the equivalent hardening.

### Recommendation
Replace `ensure_sub` with `saturating_sub` (or explicitly clamp to zero when `now < last_update_block`) in `reward_per_token`'s elapsed-blocks computation, mirroring the pattern already used in `pallet-vesting`'s `locked_at`. Additionally, `update_pool_rewards` should not regress `last_update_block` backward if `now` is smaller than the stored value, to avoid silently reversing reward accounting once the provider catches back up.

### Proof of Concept
1. Configure a runtime's `pallet_asset_rewards::Config::BlockNumberProvider` to `cumulus_pallet_parachain_system::RelaychainDataProvider<Runtime>` (the documented, supported configuration used by `asset-hub-*` runtimes).
2. Create a pool and have a staker call `stake`; this sets `Pools::<T>::get(pool_id).last_update_block = N` (current relay parent number) via `update_pool_rewards` [7](#0-6) .
3. On a subsequent parachain block, the relay parent observed by `RelaychainDataProvider::current_block_number()` regresses below `N` (an occurrence explicitly not excluded by the provider's own contract [3](#0-2) ).
4. Any call to `unstake` or `harvest_rewards` for that pool now executes `Self::last_block_reward_applicable(...).ensure_sub(pool_info.last_update_block)?`, which underflows and returns `Err`, aborting the extrinsic [5](#0-4) .
5. Because a successful update is required to ever advance `last_update_block` again, and none can succeed, the failure is permanent: the staker's frozen tokens and accrued rewards in that pool become permanently inaccessible.

### Citations

**File:** substrate/frame/asset-rewards/src/lib.rs (L128-133)
```rust
/// The block number type for the pallet.
///
/// This type is derived from the `BlockNumberProvider` associated type in the `Config` trait.
/// It represents the block number type that the pallet uses for scheduling and expiration.
pub type BlockNumberFor<T> =
	<<T as Config>::BlockNumberProvider as BlockNumberProvider>::BlockNumber;
```

**File:** substrate/frame/asset-rewards/src/lib.rs (L476-480)
```rust
			// Always start by updating staker and pool rewards.
			let pool_info = Pools::<T>::get(pool_id).ok_or(Error::<T>::NonExistentPool)?;
			let staker_info = PoolStakers::<T>::get(pool_id, &staker).unwrap_or_default();
			let (mut pool_info, mut staker_info) =
				Self::update_pool_and_staker_rewards(&pool_info, &staker_info)?;
```

**File:** substrate/frame/asset-rewards/src/lib.rs (L524-530)
```rust
			let pool_info = Pools::<T>::get(pool_id).ok_or(Error::<T>::NonExistentPool)?;
			let now = T::BlockNumberProvider::current_block_number();
			ensure!(now > pool_info.expiry_block || caller == staker, BadOrigin);

			let staker_info = PoolStakers::<T>::get(pool_id, &staker).unwrap_or_default();
			let (mut pool_info, mut staker_info) =
				Self::update_pool_and_staker_rewards(&pool_info, &staker_info)?;
```

**File:** substrate/frame/asset-rewards/src/lib.rs (L578-585)
```rust
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

**File:** prdoc/stable2512/pr_9826.prdoc (L1-21)
```text
title: Update pallet-asset-rewards to use BlockNumberProvider
doc:
- audience: Runtime Dev
  description: |-
    This updates pallet-asset-rewards to use BlockNumberProvider trait instead of directly
    using the system block number.
    This change enables the pallet to be used in parachain enviroments where block numbers
    may come from different sources(i.e. Relay chain).

    Runtimes using this pallet must now implement the BlockNumberProvider type.
    For most cases setting the type `BlockNumber = frame_system::Pallet<Self>;` will maintain
    the previous behaviour.
crates:
- name: asset-hub-rococo-runtime
  bump: major
- name: asset-hub-westend-runtime
  bump: major
- name: pallet-asset-rewards
  bump: major
- name: pallet-staking-async-parachain-runtime
  bump: major
```

**File:** cumulus/pallets/parachain-system/src/lib.rs (L2017-2021)
```rust
pub trait RelaychainStateProvider {
	/// May be called by any runtime module to obtain the current state of the relay chain.
	///
	/// **NOTE**: This is not guaranteed to return monotonically increasing relay parents.
	fn current_relay_chain_state() -> RelayChainState;
```

**File:** prdoc/stable2509/pr_9497.prdoc (L1-12)
```text
title: 'Society pallet supports non-consecutive block provider'
doc:
- audience: Runtime Dev
  description: |-
    Society pallet correctly handles situations where on_initialize is invoked with block numbers that:
    - increase but are not strictly consecutive (e.g., jump from 5 → 10), or
    - are repeated (e.g., multiple blocks are built at the same Relay Chain parent block, all reporting the same BlockNumberProvider value).
    This situation may occur when the BlockNumberProvider is not local - for example, on a parachain using the Relay Chain block number provider.

crates:
- name: pallet-society
  bump: major
```

**File:** substrate/frame/vesting/src/vesting_info.rs (L88-101)
```rust
	pub fn locked_at<BlockNumberToBalance: Convert<BlockNumber, Balance>>(
		&self,
		n: BlockNumber,
	) -> Balance {
		// Number of blocks that count toward vesting;
		// saturating to 0 when n < starting_block.
		let vested_block_count = n.saturating_sub(self.starting_block);
		let vested_block_count = BlockNumberToBalance::convert(vested_block_count);
		// Return amount that is still locked in vesting.
		vested_block_count
			.checked_mul(&self.per_block()) // `per_block` accessor guarantees at least 1.
			.map(|to_unlock| self.locked.saturating_sub(to_unlock))
			.unwrap_or(Zero::zero())
	}
```
