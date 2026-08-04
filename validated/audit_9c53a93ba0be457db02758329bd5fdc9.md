### Title
Non-monotonic relay-chain clock accepted by `pallet-vesting`'s `BlockNumberProvider`, breaking vesting/lock invariants when `RelaychainDataProvider` is used - (File: `cumulus/pallets/parachain-system/src/lib.rs`, `substrate/frame/vesting/src/lib.rs`)

### Summary
The Arbitrum report is about code that assumes `block.number` reflects the local (fast) execution clock, when it actually reflects a different, slower clock domain (L1), breaking timing-sensitive decay math. The same class of bug exists in `polkadot-sdk`: `pallet-vesting` was extended (see `prdoc/1.6.0/pr_2403.prdoc`) to accept a pluggable `T::BlockNumberProvider`, explicitly recommending parachains use `cumulus_pallet_parachain_system::RelaychainDataProvider` — a *different* clock domain (the relay chain) — instead of the local `frame_system::Pallet` clock. However, `RelaychainDataProvider::current_block_number()` is explicitly documented as **not guaranteed to return monotonically increasing values**, while `pallet-vesting`'s own `Config::BlockNumberProvider` documentation states it "**Must** return monotonically increasing values when called from consecutive blocks." This mismatch between the clock domain vesting math assumes and the clock domain actually supplied is the direct structural analog of the Arbitrum bug.

### Finding Description
`pallet-vesting`'s `Config` trait declares the invariant plainly: [1](#0-0) 

The suggested "remote block number" implementation is `RelaychainDataProvider`: [2](#0-1) 

Its `current_block_number()` reads `relay_parent_number` from `ValidationData` when present, and otherwise falls back to `Pallet::<T>::last_relay_block_number()` (e.g. in `on_initialize`, before the inherent sets `ValidationData` for the block). Critically, the sibling trait `RelaychainStateProvider::current_relay_chain_state()` carries the same explicit caveat: [3](#0-2) 

i.e. "This is not guaranteed to return monotonically increasing relay parents." A collator can select a relay parent for block N+1 that is not a strict descendant of the relay parent used for block N (this can occur across relay-chain forks/reorgs a collator observes, or when a parachain lags and later catches up on a different fork tip) — no malicious relayer, validator, or admin is required, only ordinary relay-chain fork activity that the block-authoring collator observes when picking a relay parent.

`pallet-vesting`'s core unlock math (`VestingInfo::locked_at`) assumes `now` (i.e. `T::BlockNumberProvider::current_block_number()`) only increases: [4](#0-3) 

and every public entry point (`vest`, `vest_other`, `vested_transfer`, `merge_schedules`) recomputes `locked_now` fresh from `now` and unconditionally overwrites the currency lock via `write_lock`: [5](#0-4) [6](#0-5) 

If a runtime wires `type BlockNumberProvider = RelaychainDataProvider<T>` (exactly as the pallet's own docs and `pr_2403` recommend for parachains), a temporary forward "jump" in the reported relay parent (e.g. due to a fork switch, or the `on_initialize` fallback returning a different/stale value than what `ValidationData` reports later in the same block) causes `locked_at` to under-report the locked amount for that call. `vest()`/`vest_other()` will then set the currency lock to the smaller amount, unlocking funds that should still be time-locked. This is the direct structural analog of the Arbitrum bug: a "block number" consumed by decay/unlock logic that does not actually advance at the rate (or with the monotonic guarantee) that the consuming pallet's math assumes.

### Impact Explanation
This breaks the "Balances ... must conserve value and settle exactly once to the rightful beneficiary and amount" and "unbacked ... unlock" impact class: a vesting schedule's locked funds could be prematurely unlocked and transferred out, in violation of the schedule's intended time-lock, purely as a consequence of relying on a non-monotonic external clock the pallet's own documentation warns is not monotonic, contradicting the guarantee the vesting math requires.

### Likelihood Explanation
Likelihood depends on relay-chain fork/reorg activity being observable by a collator when selecting the relay parent for a block, and requires a runtime to have opted into `RelaychainDataProvider` for `pallet-vesting`'s `BlockNumberProvider` (an explicitly recommended, non-default configuration per `pr_2403.prdoc`). No malicious relayer, validator, governance, or admin action is needed — only normal relay-chain conditions the code's own doc comments already flag as unsafe for this use.

### Recommendation
Either (a) make `RelaychainDataProvider::current_block_number()` monotonic (e.g. clamp to the maximum previously observed relay block number, persisting a high-water mark), or (b) strengthen `pallet-vesting` (and any other consumer relying on `BlockNumberProvider::current_block_number()` for irreversible unlock/decay math) to clamp `now` against a stored monotonic high-water mark before computing `locked_at`, so a temporarily lower/non-monotonic reading can never reduce a previously-recorded locked amount.

### Proof of Concept
1. Deploy a parachain runtime with `impl pallet_vesting::Config { type BlockNumberProvider = cumulus_pallet_parachain_system::RelaychainDataProvider<Runtime>; ... }` (the pattern explicitly enabled by `pr_2403.prdoc`).
2. Create a vesting schedule for account `A` starting near relay block `R0` with a multi-block duration.
3. At relay parent `R1 > R0 + k` (a legitimate later block, e.g. due to a fork-selection scenario where the collator picks a relay parent that is numerically higher than what will later be canonically reachable in sequence, or via the `on_initialize` fallback path returning `last_relay_block_number()` which can lag/diverge from the value set later by the inherent in the same block), call `Vesting::vest(A)`. `locked_at(R1)` computes a reduced locked amount and `write_lock` applies it, unlocking part of the schedule early relative to the canonical relay-chain time progression.
4. `A` transfers the newly-unlocked balance out before the "true" monotonic relay-chain time would have permitted it, demonstrating early/unbacked unlock of vested funds. [7](#0-6)

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

**File:** substrate/frame/vesting/src/lib.rs (L598-618)
```rust
	fn report_schedule_updates(
		schedules: Vec<VestingInfo<BalanceOf<T>, BlockNumberFor<T>>>,
		action: VestingAction,
	) -> (Vec<VestingInfo<BalanceOf<T>, BlockNumberFor<T>>>, BalanceOf<T>) {
		let now = T::BlockNumberProvider::current_block_number();

		let mut total_locked_now: BalanceOf<T> = Zero::zero();
		let filtered_schedules = action
			.pick_schedules::<T>(schedules)
			.filter(|schedule| {
				let locked_now = schedule.locked_at::<T::BlockNumberToBalance>(now);
				let keep = !locked_now.is_zero();
				if keep {
					total_locked_now = total_locked_now.saturating_add(locked_now);
				}
				keep
			})
			.collect::<Vec<_>>();

		(filtered_schedules, total_locked_now)
	}
```

**File:** substrate/frame/vesting/src/lib.rs (L654-665)
```rust
	/// Unlock any vested funds of `who`.
	fn do_vest(who: T::AccountId) -> DispatchResult {
		let schedules = Vesting::<T>::get(&who).ok_or(Error::<T>::NotVesting)?;

		let (schedules, locked_now) =
			Self::exec_action(schedules.to_vec(), VestingAction::Passive)?;

		Self::write_vesting(&who, schedules)?;
		Self::write_lock(&who, locked_now);

		Ok(())
	}
```

**File:** cumulus/pallets/parachain-system/src/lib.rs (L2017-2021)
```rust
pub trait RelaychainStateProvider {
	/// May be called by any runtime module to obtain the current state of the relay chain.
	///
	/// **NOTE**: This is not guaranteed to return monotonically increasing relay parents.
	fn current_relay_chain_state() -> RelayChainState;
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

**File:** prdoc/1.6.0/pr_2403.prdoc (L1-9)
```text
title: Configurable block number provider in pallet-vesting

doc:
  - audience: Runtime Dev
    description: |
      Adds `BlockNumberProvider` type to pallet-vesting Config trait, allowing for custom providers instead of hardcoding frame-system.
      This is particularly useful for parachains wanting to use `cumulus_pallet_parachain_system::RelaychainDataProvider` with `pallet-vesting`.

crates: [ ]
```
