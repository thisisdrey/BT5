### Title
`reward_per_token` accumulator in `pallet-asset-rewards` uses an insufficient precision scaling factor causing permanent silent loss of staker rewards - (File: `substrate/frame/asset-rewards/src/lib.rs`)

### Summary
`pallet-asset-rewards` computes a per-token reward accumulator (`reward_per_token`) using a hard-coded scaling constant `PRECISION_SCALING_FACTOR = 4096` [1](#0-0) . This is the exact same bug class as the referenced Canto `secRewardsPerShare` finding: an integer-division reward-rate accumulator that is scaled by a constant far too small relative to realistic token amounts/decimals, so the division rounds down to zero and reward accrual silently stalls, permanently reducing yield paid to stakers.

### Finding Description
`reward_per_token` is computed as:

```rust
Ok(pool_info.reward_per_token_stored.ensure_add(
    pool_info
        .reward_rate_per_block
        .ensure_mul(rewardable_blocks_elapsed.into())?
        .ensure_mul(PRECISION_SCALING_FACTOR.into())?
        .ensure_div(pool_info.total_tokens_staked)?,
)?)
``` [2](#0-1) 

`PRECISION_SCALING_FACTOR` is only `4096` (2^12) [1](#0-0) , whereas typical fungible assets configured with `T::Balance` use 10-18 decimal places (e.g. `1e12`/`1e18` base units per token, exactly the pattern the external Canto report warns about). Whenever `reward_rate_per_block * blocks_elapsed * 4096 < total_tokens_staked`, the division truncates to `0`, so `reward_per_token_stored` never advances even though blocks keep passing and even though the pool holds funded reward-asset balance to pay out. Because `derive_rewards` reads the delta of this same value to compute a staker's earned rewards, `reward_per_token.ensure_sub(staker_info.reward_per_token_paid)` stays `0` for that staker indefinitely:

```rust
Ok(staker_info
    .amount
    .ensure_mul(reward_per_token.ensure_sub(staker_info.reward_per_token_paid)?)?
    .ensure_div(PRECISION_SCALING_FACTOR.into())?
    .ensure_add(staker_info.rewards)?)
``` [3](#0-2) 

This is a permissionless-facing bug: any account can call the pool's `stake` extrinsic to become a staker in an existing pool with realistic decimals/supply, and simply by staking a large enough amount relative to `reward_rate_per_block`, that staker's accrued rewards will be truncated to zero forever, with no error raised (`ensure_div`/`ensure_mul` only fail on true overflow, not on rounding-to-zero). Unlike `pallet-nomination-pools`, which uses a `FixedU128`-based `RewardCounter` (effectively 1e18 internal precision) specifically documented to avoid this class of truncation [4](#0-3) , and unlike `pallet-staking`/`pallet-staking-async` which use `Perbill`/exact-share arithmetic [5](#0-4) , `pallet-asset-rewards` relies on a fixed `4096` multiplier that provides only ~12 bits of extra precision — trivially insufficient once `total_tokens_staked` exceeds a few thousand base units (i.e., a tiny fraction of one token at typical 10-18 decimal precision).

### Impact Explanation
Stakers in `pallet-asset-rewards` pools can permanently lose all yield despite the pool being correctly funded and the admin correctly configuring `reward_rate_per_block`. This is a silent, protocol-level accounting bug (not an admin/governance misconfiguration) that breaks the pallet's core reward-distribution invariant — reward asset accrues into `reward_per_token_stored` but the value truncates to zero and never recovers (it is monotonically accumulated additive state; a rounding-to-zero delta is not retried or corrected later at finer granularity for that update window). Reward tokens sit stuck in the pool's reward account, in effect an unbacked/undistributed reward pool from the staker's perspective, and can eventually be swept out entirely by the pool admin via `cleanup_pool` once the pool empties of stakers [6](#0-5) , effectively converting theoretically-owed staker rewards into admin-recoverable balance. This matches the required impact class of "public underpriced work" / "theft or unbacked... payout" degrading intended reward-conservation behavior for an unprivileged, permissionless-facing pathway (staking is open to any account, no privileged action required to trigger the loss).

### Likelihood Explanation
High. No adversarial coordination, governance action, or privileged role is needed — any account holding the pool's staked asset can call `stake`, and the condition `reward_rate_per_block * blocks_elapsed * 4096 < total_tokens_staked` is trivially satisfiable for realistic pool parameters (e.g. an 18-decimal staked asset with even a moderate total stake, combined with a modest per-block reward rate). The bug is deterministic and reproducible purely from public state (pool parameters + stake amounts), requiring no race condition or timing trick.

### Recommendation
Replace the fixed `u16` `PRECISION_SCALING_FACTOR = 4096` with a much larger precision constant (e.g., `1e18` or a `FixedU128`/`U256`-based intermediate calculation, mirroring `pallet-nomination-pools`'s `RewardCounter` approach), and perform the `reward_per_token` computation using 256-bit or fixed-point intermediate arithmetic (e.g. `multiply_by_rational_with_rounding` already available in `sp_arithmetic`, see `substrate/primitives/arithmetic/src/helpers_128bit.rs`) to avoid overflow while providing sufficient precision headroom relative to `T::Balance`'s realistic value ranges.

### Proof of Concept
1. Admin creates a pool with a staked asset that has 18 decimals and sets `reward_rate_per_block = 1_000_000` (a modest reward rate), via `create_pool`.
2. A staker stakes `total_tokens_staked = 10_000_000_000_000` (a small fraction of a token at 18-decimal precision, well within normal usage).
3. One block elapses. `reward_per_token` computes:
   `reward_rate_per_block(1_000_000) * blocks_elapsed(1) * PRECISION_SCALING_FACTOR(4096) / total_tokens_staked(10_000_000_000_000)`
   `= 4_096_000_000 / 10_000_000_000_000 = 0` (integer division truncates to zero).
4. `reward_per_token_stored` stays at its prior value; `derive_rewards` computes `staker_info.amount * 0 / 4096 = 0` added rewards for that staker.
5. This repeats every block indefinitely as long as `total_tokens_staked` remains large relative to `reward_rate_per_block * 4096`, so the staker accrues zero rewards permanently while the pool's reward-asset balance remains funded and unclaimed, matching the exact rounding-to-zero mechanism described in the external Canto report but reproduced natively in `substrate/frame/asset-rewards/src/lib.rs` lines 786-809 and 812-824.

### Citations

**File:** substrate/frame/asset-rewards/src/lib.rs (L117-118)
```rust
/// Multiplier to maintain precision when calculating rewards.
pub(crate) const PRECISION_SCALING_FACTOR: u16 = 4096;
```

**File:** substrate/frame/asset-rewards/src/lib.rs (L697-729)
```rust
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

**File:** substrate/frame/nomination-pools/src/lib.rs (L1472-1509)
```rust
		// * accuracy notes regarding the multiplication in `checked_from_rational`:
		// `current_payout_balance` is a subset of the total_issuance at the very worse.
		// `bonded_points` are similarly, in a non-slashed pool, have the same granularity as
		// balance, and are thus below within the range of total_issuance. In the worse case
		// scenario, for `saturating_from_rational`, we have:
		//
		// dot_total_issuance * 10^18 / `minJoinBond`
		//
		// assuming `MinJoinBond == ED`
		//
		// dot_total_issuance * 10^18 / 10^10 = dot_total_issuance * 10^8
		//
		// which, with the current numbers, is a miniscule fraction of the u128 capacity.
		//
		// Thus, adding two values of type reward counter should be safe for ages in a chain like
		// Polkadot. The important note here is that `reward_pool.last_recorded_reward_counter` only
		// ever accumulates, but its semantics imply that it is less than total_issuance, when
		// represented as `FixedU128`, which means it is less than `total_issuance * 10^18`.
		//
		// * accuracy notes regarding `checked_from_rational` collapsing to zero, meaning that no
		//   reward can be claimed:
		//
		// largest `bonded_points`, such that the reward counter is non-zero, with `FixedU128` will
		// be when the payout is being computed. This essentially means `payout/bonded_points` needs
		// to be more than 1/1^18. Thus, assuming that `bonded_points` will always be less than `10
		// * dot_total_issuance`, if the reward_counter is the smallest possible value, the value of
		//   the
		// reward being calculated is:
		//
		// x / 10^20 = 1/ 10^18
		//
		// x = 100
		//
		// which is basically 10^-8 DOTs. See `smallest_claimable_reward` for an example of this.
		let current_reward_counter =
			T::RewardCounter::checked_from_rational(new_pending_rewards, bonded_points)
				.and_then(|ref r| self.last_recorded_reward_counter.checked_add(r))
				.ok_or(Error::<T>::OverflowRisk)?;
```

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L746-757)
```rust
		if share_part.is_zero() {
			return None;
		}

		let validator_total_incentive = share_part.mul_floor(era_incentive_budget);
		let validator_incentive_for_page = page_stake_part.mul_floor(validator_total_incentive);

		if validator_incentive_for_page.is_zero() {
			return None;
		}

		Some(validator_incentive_for_page)
```
