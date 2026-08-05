### Title
Era reward pot slot reuse can permanently drain/overwrite unclaimed staker rewards if `HistoryDepth` is not kept below the hard-coded `POT_POOL_SIZE` at runtime - ([File: substrate/frame/staking-async/src/reward.rs])

### Summary
`pallet-staking-async` replaced per-era reward pot accounts with a **fixed-size rotating pool** of `POT_POOL_SIZE = 200` accounts, addressed by `era % POT_POOL_SIZE` [1](#0-0) . This is structurally the same bug class as the reported `ecosystemVesting` array: a fixed-length container is indexed by a monotonically increasing counter (months → eras), and correctness depends entirely on an assumption that the index never wraps around into a still-needed slot. The safety of this design is documented as resting on a single invariant — `POT_POOL_SIZE > HistoryDepth` — enforced only by an `integrity_test` [2](#0-1) , rather than by a runtime check performed every time an era pot is created, drained, or claimed against.

### Finding Description
`EraRewardManager::create`, `snapshot_era_rewards`, `drain`, and `cleanup_era` all resolve the era pot account via `T::RewardPots::pot_account(RewardPot::Era(era, kind))`, which internally maps `era` to a slot with `era % POT_POOL_SIZE` [3](#0-2) . The doc comment explicitly states the danger: "a future era that reuses the same slot finds an existing zero-balance account and snapshots into it," and that this is only safe "after its previous era has been pruned and drained" [2](#0-1) .

`drain`/`cleanup_era` unconditionally withdraws the *entire* remaining balance of whatever account currently occupies a slot and sends it to `UnclaimedRewardHandler` [4](#0-3) , and `snapshot_era_rewards` transfers new funds into that same slot for the new era, with no check that the slot's *previous occupant era* has actually had its rewards claimed [5](#0-4) . The only guard against a slot being reused while an older era's rewards are still outstanding is the compile/startup-time `integrity_test` assertion `POT_POOL_SIZE > HistoryDepth`. This is exactly analogous to the reported Solidity bug: the fixed array (`ecosystemVesting[50]` / the 200-slot pot pool) is trusted to always be indexed within bounds by an external invariant (elapsed months ≤ 50 / `HistoryDepth` < `POT_POOL_SIZE`), but nothing in the hot path (`_calculateAvailablePercentage` / `create`/`drain`/`snapshot_era_rewards`) re-validates that invariant at the point of use.

`HistoryDepth` governs how far back stakers may call `payout_stakers` to claim a past era's rewards; if it is ever set (via root/governance storage mutation, migration, or a future config change) to a value `>= POT_POOL_SIZE = 200`, or if era advancement outpaces the assumed pruning cadence, a staker whose era `E` reward pot has not yet been drained/claimed can have that exact slot recycled by era `E + 200`, silently overwriting or draining their pot before `payout_stakers` is called for era `E`.

### Impact Explanation
If the `POT_POOL_SIZE > HistoryDepth` invariant is ever violated at runtime (config drift, migration, or a future extrinsic that adjusts `HistoryDepth` without re-checking this hard-coded constant), stakers who have not yet claimed rewards for an older era permanently lose access to those rewards: the pot backing their claimable era is drained to `UnclaimedRewardHandler` and/or overwritten by a newer era's snapshot before they call `payout_stakers`. This is an unbacked-loss / permanent-fund-lock condition for legitimate stakers with no attacker action required, matching the "permanent user-fund lock" and "duplicate settlement" impact classes in scope.

### Likelihood Explanation
Likelihood is moderate-to-low under the current default parameters, because the safety invariant is intentionally checked once via `integrity_test` at pallet build. However, `integrity_test` runs only at genesis/build time, not on every era rotation or whenever `HistoryDepth` changes, so there is no runtime re-validation. Since `POT_POOL_SIZE` is a hard-coded constant (`= 200`) inside `pallet-staking-async`, any future parameter change, migration, or configuration mistake that raises effective `HistoryDepth` to 200 or more (or any scenario where claim latency for a given era exceeds 200 eras) reintroduces this exact class of bug without any additional runtime defense.

### Recommendation
Add a runtime-checked guard at the point of use rather than relying solely on the build-time `integrity_test`:
- In `EraRewardManager::create`/`snapshot_era_rewards`, before reusing a slot, verify that the era currently occupying that slot (if any) has actually been drained/claimed, and defensively refuse (or log/alert) instead of silently overwriting.
- Alternatively, assert `HistoryDepth < POT_POOL_SIZE` on every `HistoryDepth` mutation path (not just at genesis), so a future config change cannot silently break the invariant.

### Proof of Concept
1. Advance the chain such that `HistoryDepth` (the effective claim window for `payout_stakers`) is configured or migrated to a value `>= POT_POOL_SIZE (200)`, or otherwise arrange for a staker's payout for era `E` to remain unclaimed for `>= 200` further eras.
2. Let era rotation proceed to era `E + 200`. `EraRewardManager::create`/`snapshot_era_rewards` for era `E + 200` resolves to the same pot account as era `E` (`era % POT_POOL_SIZE`) [3](#0-2) , and `cleanup_era`/`drain` for era `E` (or the recycling logic itself) withdraws/overwrites the pot's balance [4](#0-3) .
3. The staker then calls `payout_stakers` for era `E`; the underlying pot account no longer holds era `E`'s reward balance (it has been drained to `UnclaimedRewardHandler` and/or refunded with era `E+200`'s snapshot), so the staker cannot recover the originally earned reward — a permanent loss analogous to the out-of-bounds/irrecoverable state in the reported `ecosystemVesting` bug.

### Citations

**File:** substrate/frame/staking-async/src/lib.rs (L593-600)
```rust
/// reused after its previous era has been pruned and drained. The
/// [`integrity_test`] enforces this invariant at runtime startup.
pub(crate) const POT_POOL_SIZE: u32 = 200;

/// Maps an era index to its slot in the rotating pot pool.
pub(crate) fn pot_slot(era: EraIndex) -> u32 {
	era % POT_POOL_SIZE
}
```

**File:** substrate/frame/staking-async/src/reward.rs (L22-29)
```rust
//!
//! Era pots are backed by a rotating pool of `POT_POOL_SIZE` accounts
//! addressed by `era % POT_POOL_SIZE`. Once created, a slot's account is kept
//! alive forever — at the end of each era's history window, its remaining
//! balance is drained to [`crate::Config::UnclaimedRewardHandler`] but the
//! provider reference is retained. A future era that reuses the same slot
//! finds an existing zero-balance account and snapshots into it. This bounds
//! the storage footprint contributed by era pots to a constant.
```

**File:** substrate/frame/staking-async/src/reward.rs (L88-97)
```rust
	pub(crate) fn snapshot_era_rewards(era: EraIndex) -> EraRewardAllocation<BalanceOf<T>> {
		let staker_era_pot = Self::create(era, RewardKind::StakerRewards);
		let incentive_era_pot = Self::create(era, RewardKind::ValidatorSelfStake);

		let general_staker_pot =
			T::RewardPots::pot_account(RewardPot::General(RewardKind::StakerRewards));
		let general_incentive_pot =
			T::RewardPots::pot_account(RewardPot::General(RewardKind::ValidatorSelfStake));

		// Leave ED in the general pots to keep them alive.
```

**File:** substrate/frame/staking-async/src/reward.rs (L161-203)
```rust
	pub(crate) fn drain(era: EraIndex, kind: RewardKind) {
		let pot_account = T::RewardPots::pot_account(RewardPot::Era(era, kind));

		// Skip if pot was never created (legacy mode doesn't create pots).
		if frame_system::Pallet::<T>::providers(&pot_account) == 0 {
			return;
		}

		let remaining = T::Currency::balance(&pot_account);

		if remaining.is_zero() {
			return;
		}

		match T::Currency::withdraw(
			&pot_account,
			remaining,
			Precision::BestEffort,
			Preservation::Expendable,
			Fortitude::Force,
		) {
			Ok(credit) => {
				T::UnclaimedRewardHandler::on_unbalanced(credit);
				log!(
					debug,
					"Drained {:?} unclaimed rewards from era {:?} {:?} pot",
					remaining,
					era,
					kind
				);
			},
			Err(e) => {
				defensive!("Failed to withdraw unclaimed rewards from era pot");
				log!(
					error,
					"Era {:?} {:?}: unclaimed reward withdrawal failed: {:?}",
					era,
					kind,
					e
				);
			},
		}
	}
```
