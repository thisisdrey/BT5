Given the tool budget is exhausted, here is my analysis based on confirmed evidence.

### Title
Vesting schedules computed in local parachain block numbers unlock at twice the intended rate after Async Backing halves block time - (File: `substrate/frame/vesting/src/lib.rs`, `docs/sdk/src/guides/async_backing_guide.rs`)

### Summary
The external report's core broken invariant is: a monotonic "block number" counter is treated as a reliable proxy for wall-clock time, but the counter's real-world cadence can change (L1 sync lag, one-block-per-tx), silently accelerating or distorting all time-based logic derived from it. The direct Polkadot SDK analog is `pallet_vesting`, whose lock-release math is expressed purely in block-number units via `T::BlockNumberProvider`, combined with the SDK's own documented Async Backing migration, which halves parachain block time from 12s to 6s.

### Finding Description
`pallet_vesting::Config` exposes a `BlockNumberProvider` used to compute `starting_block`, `per_block`, and the unlock amount at any given "now": [1](#0-0) . The per-block unlock rate (`per_block`) is fixed at schedule-creation time as a `Balance`-per-*block* quantity, not per unit of wall-clock time: [2](#0-1) , and `locked_at` walks `n - starting_block` blocks to determine how much remains locked: [3](#0-2) .

For parachains, the officially documented "Suggested values" for `BlockNumberProvider` recommend keeping `frame_system::Pallet` (the local, parachain-native block counter) for chains that already have the pallet deployed: [4](#0-3) . The Polkadot SDK's own Async Backing migration guide explicitly documents that enabling async backing changes parachain block production from 12s to 6s blocks, and warns that "any on-chain logic that derives time from parachain block numbers will see time move twice as fast," specifically calling out vesting schedules and unlock periods as at risk, and recommending switching timing to the relay chain block number or `pallet_timestamp` instead: [5](#0-4) . This mirrors the CLOCK_MODE()/L2 report exactly: a block-number-based clock is assumed to tick at a fixed real-world rate, but the platform (sequencer resync interval, or here, the collator/relay slot cadence) can change that rate, so all durations computed in block units silently compress or stretch relative to real time.

The `RelaychainDataProvider<T>` was added precisely to give pallets a block-number source immune to local block-time changes: [6](#0-5) , and PRs for `pallet-conviction-voting`, `pallet-referenda`, `pallet-scheduler`, and `pallet-society` were all updated to make this provider configurable for exactly this reason: [7](#0-6) [8](#0-7) [9](#0-8) . However, `pallet_vesting`'s own migration note admits "There is currently no migration provided to 'hot-swap' block number providers and it may result in undefined behavior when doing so," and defaults already-deployed chains to the local (parachain) provider: [4](#0-3) . This means a parachain that already has vesting schedules locked using the local block counter, and later follows the SDK's own documented Async Backing rollout guide (halving block time), will have every existing vesting schedule's remaining lock duration cut in half in wall-clock terms — funds beneficiaries were promised would unlock over N months instead unlock in N/2 months, with no code path re-normalizing `per_block`/`starting_block` for the new block cadence.

### Impact Explanation
This breaks the "conserve value and settle exactly once to the rightful beneficiary and amount" invariant for vesting: the *rate* of value release from locked balances is silently altered as a side effect of an unrelated protocol upgrade (enabling async backing), without any explicit governance action re-authorizing a different release schedule. Vesting beneficiaries gain early access to funds intended to be time-locked over a longer real-world duration, and any external commitments, cliffs, or legal/economic assumptions tied to the original schedule are violated. Because vesting schedules are commonly used for team/investor lock-ups, this is a fund-timing integrity issue at the chain level, not a peer/relayer/admin-abuse issue.

### Likelihood Explanation
This does not require a malicious actor at all — it is triggered purely by the chain's normal operational lifecycle: any parachain operator upgrading a chain with active vesting schedules and local (`frame_system`)-based `BlockNumberProvider` who follows the SDK's own Async Backing guide (a standard, encouraged upgrade path) as-is will trigger this discrepancy for every account with an in-flight vesting schedule at the time of the upgrade, since there is explicitly no migration path.

### Recommendation
- Provide (or require) a migration when block time changes (e.g., enabling async backing) that re-derives `per_block` and `starting_block`/`ending_block` for all existing `VestingInfo` entries to preserve original wall-clock unlock timing, analogous to the `ConvertBlockNumber` migration helper already built for `pallet-core-fellowship`: [10](#0-9) .
- Alternatively/additionally, default `pallet_vesting::BlockNumberProvider` to a time-based or relay-chain-derived source for any parachain adopting async backing, and add an explicit block-time-change check/guard that fails migrations if pending vesting schedules exist without remediation.

### Proof of Concept
1. Deploy a parachain with `pallet_vesting::Config::BlockNumberProvider = frame_system::Pallet<Runtime>` and 12s block time (sync backing).
2. Create a vesting schedule via `vested_transfer` for `duration = D` blocks, i.e., ~`D * 12s` real time: [11](#0-10) .
3. Follow the documented Async Backing Phase 3 steps to enable async backing and set `MILLISECS_PER_BLOCK = 6000`: [12](#0-11) .
4. Observe that the same `D` blocks now elapse in ~`D * 6s`, so `locked_at` reaches zero (full unlock) in half the originally promised wall-clock time, with no compensating migration having been applied: [3](#0-2) [5](#0-4) .

**Note on limitations**: I was not able to verify, within the remaining tool budget, the exact `BlockNumberProvider` value configured for `pallet_vesting` in a specific live runtime (e.g., asset-hub-westend) or definitively confirm whether that specific chain enabled async backing after vesting schedules were already in flight. The vulnerability is demonstrated at the structural/pallet level and is explicitly acknowledged as a risk by the SDK's own documentation; confirming a concrete affected production runtime would require further investigation with a full Devin session that can read complete runtime configs and git history.

### Citations

**File:** substrate/frame/vesting/src/lib.rs (L183-205)
```rust
		/// Query the current block number.
		///
		/// Must return monotonically increasing values when called from consecutive blocks.
		/// Can be configured to return either:
		/// - the local block number of the runtime via `frame_system::Pallet`
		/// - a remote block number, eg from the relay chain through `RelaychainDataProvider`
		/// - an arbitrary value through a custom implementation of the trait
		///
		/// There is currently no migration provided to "hot-swap" block number providers and it may
		/// result in undefined behavior when doing so. Parachains are therefore best off setting
		/// this to their local block number provider if they have the pallet already deployed.
		///
		/// Suggested values:
		/// - Solo- and Relay-chains: `frame_system::Pallet`
		/// - Parachains that may produce blocks sparingly or only when needed (on-demand):
		///   - already have the pallet deployed: `frame_system::Pallet`
		///   - are freshly deploying this pallet: `RelaychainDataProvider`
		/// - Parachains with a reliably block production rate (PLO or bulk-coretime):
		///   - already have the pallet deployed: `frame_system::Pallet`
		///   - are freshly deploying this pallet: no strong recommendation. Both local and remote
		///     providers can be used. Relay provider can be a bit better in cases where the
		///     parachain is lagging its block production to avoid clock skew.
		type BlockNumberProvider: BlockNumberProvider<BlockNumber = BlockNumberFor<Self>>;
```

**File:** substrate/frame/vesting/src/lib.rs (L719-745)
```rust
	fn vested_transfer(
		source: &T::AccountId,
		dest: &T::AccountId,
		amount: BalanceOf<T>,
		duration: BlockNumberFor<T>,
		start_at: Option<BlockNumberFor<T>>,
	) -> DispatchResult {
		if amount.is_zero() {
			return Ok(());
		}

		if duration.is_zero() {
			// Zero duration means liquid transfer with no vesting schedule.
			T::Currency::transfer(source, dest, amount, ExistenceRequirement::AllowDeath)
		} else {
			let starting_block =
				start_at.unwrap_or_else(|| T::BlockNumberProvider::current_block_number());
			let duration_as_balance = T::BlockNumberToBalance::convert(duration);
			// Round up so that vesting completes within `duration` blocks, not longer.
			let per_block =
				((amount.saturating_add(duration_as_balance).saturating_sub(One::one())) /
					duration_as_balance)
					.max(One::one());
			let schedule = VestingInfo::new(amount, per_block, starting_block);
			Self::do_vested_transfer(source, dest, schedule)
		}
	}
```

**File:** substrate/frame/vesting/src/vesting_info.rs (L87-101)
```rust
	/// Amount locked at block `n`.
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

**File:** docs/sdk/src/guides/async_backing_guide.rs (L196-221)
```rust
//! ## Phase 3 - Activate Async Backing
//!
//! This phase consists of changes to your parachain’s runtime that activate async backing feature.
//!
//! 1. Verify `pallet_aura` has `AllowMultipleBlocksPerSlot` set to `true` in `runtime/src/lib.rs`
//!    (this should already be done from Phase 1).
#![doc = docify::embed!("../../templates/parachain/runtime/src/configs/mod.rs", aura_config)]
//! 2. Verify `UNINCLUDED_SEGMENT_CAPACITY` is set to at least `3` in `runtime/src/lib.rs`.
#![doc = docify::embed!("../../templates/parachain/runtime/src/lib.rs", async_backing_params)]
//! 3. Decrease `MILLISECS_PER_BLOCK` to 6000.
//!
//! - Note: For a parachain which measures time in terms of its own block number rather than by
//!   relay block number it may be preferable to increase velocity. Changing block time may cause
//!   complications, requiring additional changes. See the section “Timing by Block Number”.
#![doc = docify::embed!("../../templates/parachain/runtime/src/lib.rs", block_times)]
//! 4. Update `MAXIMUM_BLOCK_WEIGHT` to reflect the increased time available for block production.
#![doc = docify::embed!("../../templates/parachain/runtime/src/lib.rs", max_block_weight)]
//! 5. Set `MinimumPeriod` to `0` in `pallet_timestamp`. This is required to allow multiple blocks
//!    within the same slot.
//! ```ignore
//! impl pallet_timestamp::Config for Runtime {
//!     ..
//!     type MinimumPeriod = ConstU64<0>;
//!     ..
//! }
//! ```
```

**File:** docs/sdk/src/guides/async_backing_guide.rs (L223-237)
```rust
//! ## Timing by Block Number
//!
//! With asynchronous backing, parachains produce blocks every 6 seconds rather than 12 seconds.
//! This means that any on-chain logic that derives time from parachain block numbers will see
//! time move twice as fast. This could result in expected and actual time not matching up,
//! potentially causing issues with vesting schedules, unlock periods, or other time-dependent
//! logic.
//!
//! The recommended strategy is to rely on relay chain block numbers for timing instead of
//! parachain block numbers. Relay block number is kept track of by each parachain in
//! `pallet-parachain-system` with the storage value `LastRelayChainBlockNumber`. This value can
//! be obtained and used wherever timing based on block number is needed.
//!
//! Alternatively, `pallet_timestamp` provides wall-clock time which is independent of block
//! number and is not affected by changes in block time.
```

**File:** cumulus/pallets/parachain-system/src/lib.rs (L2031-2055)
```rust
/// Implements [`BlockNumberProvider`] that returns relay chain block number fetched from validation
/// data.
///
/// When validation data is not available (e.g. within `on_initialize`), it will fallback to use
/// [`Pallet::last_relay_block_number()`].
///
/// Implements [`BlockNumberProvider`] and [`RelaychainStateProvider`] that returns relevant relay
/// data fetched from validation data.
///
/// NOTE: When validation data is not available (e.g. within `on_initialize`):
///
/// - [`current_relay_chain_state`](Self::current_relay_chain_state): Will return the default value
///   of [`RelayChainState`].
/// - [`current_block_number`](Self::current_block_number): Will return
///   [`Pallet::last_relay_block_number()`].
pub struct RelaychainDataProvider<T>(core::marker::PhantomData<T>);

impl<T: Config> BlockNumberProvider for RelaychainDataProvider<T> {
	type BlockNumber = relay_chain::BlockNumber;

	fn current_block_number() -> relay_chain::BlockNumber {
		ValidationData::<T>::get()
			.map(|d| d.relay_parent_number)
			.unwrap_or_else(|| Pallet::<T>::last_relay_block_number())
	}
```

**File:** prdoc/stable2503/pr_6338.prdoc (L1-14)
```text
# Schema: Polkadot SDK PRDoc Schema (prdoc) v1.0.0
# See doc at https://raw.githubusercontent.com/paritytech/polkadot-sdk/master/prdoc/schema_user.json

title: Update Referenda to Support Block Number Provider

doc:
  - audience: Runtime Dev
    description: |
      This PR makes the referenda pallet uses the relay chain as a block provider for a parachain on a regular schedule.
      To migrate existing referenda implementations, simply add `type BlockNumberProvider = System` to have the same behavior as before.

crates:
- name: pallet-referenda
  bump: major
```

**File:** prdoc/stable2503/pr_6621.prdoc (L1-14)
```text
# Schema: Polkadot SDK PRDoc Schema (prdoc) v1.0.0
# See doc at https://raw.githubusercontent.com/paritytech/polkadot-sdk/master/prdoc/schema_user.json

title: Update Conviction Voting Pallet to Support Block Number Provider

doc:
  - audience: Runtime Dev
    description: |
      This PR makes the block number provider used in the society pallet configurable. Before this PR, society pallet always used the system block number,
      with this PR some runtime can opt to use the relay chain block number instead.

crates:
- name: pallet-conviction-voting
  bump: major
```

**File:** prdoc/stable2503/pr_7441.prdoc (L1-25)
```text
title: 'Update Scheduler to have a configurable block number provider'
doc:
- audience: Runtime Dev
  description: |-
    This PR makes `pallet_scheduler` configurable by introducing `BlockNumberProvider` in
    `pallet_scheduler::Config`. Instead of relying solely on
    `frame_system::Pallet::<T>::block_number()`, the scheduler can now use any block number source,
    including external providers like the relay chain.

    Parachains can continue using `frame_system::Pallet::<Runtime>` without issue. To retain the
    previous behavior, set `BlockNumberProvider` to `frame_system::Pallet::<Runtime>`.

crates:
- name: collectives-westend-runtime
  bump: patch
- name: rococo-runtime
  bump: patch
- name: westend-runtime
  bump: patch
- name: pallet-democracy
  bump: patch
- name: pallet-referenda
  bump: patch
- name: pallet-scheduler
  bump: major
```

**File:** substrate/frame/core-fellowship/src/migration.rs (L133-167)
```rust
pub mod v2 {
	use super::*;
	use crate::BlockNumberFor as NewBlockNumberFor;
	use frame_system::pallet_prelude::BlockNumberFor as LocalBlockNumberFor;

	/// Converts previous (local) block number into the new one. May just be identity functions
	/// if sticking with the local block number.
	pub trait ConvertBlockNumber<L, N> {
		/// Converts to the new type and finds the equivalent moment in time as from the view of the
		/// new block provider
		///
		/// # Example usage
		///
		/// ```rust,ignore
		/// // Let's say you are a parachain and switching block providers to the relay chain.
		/// // This will return what the relay block number was at the moment the previous provider's
		/// // number was `local_moment`.
		/// fn equivalent_moment_in_time(local_moment: u32) -> u32 {
		/// 	// How long it's been since 'local_moment' from the parachains pov.
		/// 	let local_block_number = System::block_number();
		/// 	let local_duration = u32::abs_diff(local_block_number, local_moment);
		/// 	// How many blocks that is from the relay's pov.
		/// 	let relay_duration = Self::equivalent_block_duration(local_duration);
		/// 	// What the relay block number must have been at 'local_moment'.
		/// 	let relay_block_number = ParachainSystem::last_relay_block_number();
		/// 	if local_block_number >= local_moment {
		/// 		// Moment was in past.
		/// 		relay_block_number.saturating_sub(relay_duration)
		/// 	} else {
		/// 		// Moment is in future.
		/// 		relay_block_number.saturating_add(relay_duration)
		/// 	}
		/// }
		/// ```
		fn equivalent_moment_in_time(local_moment: L) -> N;
```
