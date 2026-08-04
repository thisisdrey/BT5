### Title
Unchecked ledger arithmetic in `make_payout_from_provider` allows silent overflow/desync of stake accounting - (File: `substrate/frame/staking-async/src/pallet/impls.rs`)

### Summary
The transfer-based reward payout path in `pallet-staking-async` mutates a staker's `StakingLedger.active`/`total` fields with raw, unchecked `+=` operators instead of the `checked_add`/`saturating_add` pattern used everywhere else in the ledger-mutation code paths for the exact same fields. [1](#0-0) 

### Finding Description
`make_payout_from_provider` is invoked from `payout_from_provider`, which is reached from the public, permissionless extrinsics `payout_stakers` and `payout_stakers_by_page` — both explicitly documented as callable "by any account, even if it is not one of the stakers": [2](#0-1) 

When the reward destination is `RewardDestination::Staked`, the ledger is fetched and updated with plain arithmetic:

```rust
if let Ok(mut ledger) = Self::ledger(Stash(stash.clone())) {
    ledger.active += amount;
    ledger.total += amount;
    ...
}
``` [3](#0-2) 

This is inconsistent with every other place in the codebase that mutates the same `active`/`total` `BalanceOf<T>` fields, all of which explicitly guard against overflow. For example, `do_bond_extra` in the legacy `pallet-staking` uses `checked_add` and returns `ArithmeticError::Overflow` on failure for the identical fields: [4](#0-3) 

Likewise, the project has a documented history of hardening exactly this class of bug: `pallet-staking: Converts all math operations to safe` and `pallet-staking-async: Use saturating addition for era reward points`, both explicitly aimed at removing raw arithmetic from staking-reward accounting to prevent overflow: [5](#0-4) [6](#0-5) 

`make_payout_from_provider` was added as part of the newer DAP (transfer-based, non-minting) payout path and reintroduces the exact unchecked-arithmetic pattern that the rest of the pallet has been progressively eliminated. `ledger.active`/`ledger.total` are `#[codec(compact)] BalanceOf<T>` and are persisted via `ledger.update()` without any bounds check performed beforehand: [7](#0-6) 

Because `payout_stakers`/`payout_stakers_by_page` are unpermissioned, callable repeatedly by any signed account over the full `HistoryDepth` window for every validator/era/page, and accumulate directly onto a staker's on-chain ledger without any overflow guard, this path is the closest local analog to the original report's core defect: arithmetic on financial/accounting state performed without the "rigorously checked" math that equivalent code elsewhere in the same crate applies.

### Impact Explanation
`ledger.active` and `ledger.total` are the authoritative source of truth for how much of a stash's balance is bonded/staked, used to compute voting weight, unbonding limits, and slashing bounds. A silent wraparound of these fields (which occurs on release/Wasm builds where Rust integer overflow does not panic by default) would corrupt the accounting invariant `total == active + sum(unlocking)` relied upon throughout the pallet (see `try_state` checks), potentially allowing a staker's recorded stake to be desynchronized from its real locked balance — impacting slashing correctness, voter-list weight, and unbonding accounting. This falls squarely within the "staking or asset accounting" and "runtime bugs that compromise intended behavior" impact categories.

### Likelihood Explanation
The mutation is only reachable through the permissionless `payout_stakers`/`payout_stakers_by_page` calls and requires the payee to have `RewardDestination::Staked`, both realistic/common configurations. Reaching an actual overflow of a large `Balance` type (e.g. u128) under normal token-economics is impractical, but the code path itself demonstrates the same class of missing-guard defect the external report flags: unlike the sibling code path (`do_bond_extra`) and unlike the project's own prior remediation PRs for this exact accounting struct, no `checked_add`/`saturating_add` is used here, so no defense-in-depth exists if `amount` or an already-large `active`/`total` value combine unexpectedly (e.g. in chains with smaller `Balance` types, or via repeated compounding over very long-lived accounts).

### Recommendation
Replace the raw `+=` operators in `make_payout_from_provider` with `checked_add` (returning/logging an error, consistent with `do_bond_extra`) or, at minimum, `saturating_add` (consistent with the rest of the reward-accounting code in this same crate, e.g. `register_claimed_reward`, `update_records`, and the `pr_9186` era-reward-points fix). This restores parity with every other ledger-mutating path in the pallet.

### Proof of Concept
1. Configure a runtime where `BalanceOf<T>` is a smaller integer type (or drive a stash's `ledger.active`/`total` near `BalanceOf<T>::MAX` through legitimate bonding/compounding over many eras).
2. Set the stash's `Payee` to `RewardDestination::Staked`.
3. Call the public extrinsic `payout_stakers`/`payout_stakers_by_page` for an era/validator where this stash has a pending reward, from any signed account (no special permission required):
   `Staking::payout_stakers(RuntimeOrigin::signed(any_account), validator_stash, era)`
4. Inside `make_payout_from_provider`, `ledger.active += amount; ledger.total += amount;` executes without a `checked_add`, so if the sum exceeds the type's max value the operation wraps (release/Wasm) or panics (debug/instrumented), rather than saturating or returning a controlled error as `do_bond_extra` does for the identical fields — breaking the pallet's `total == active + unlocking` invariant.

### Citations

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L618-627)
```rust
		// For Staked destination, update ledger.
		if matches!(dest, RewardDestination::Staked) {
			if let Ok(mut ledger) = Self::ledger(Stash(stash.clone())) {
				ledger.active += amount;
				ledger.total += amount;
				let _ = ledger
					.update()
					.defensive_proof("ledger fetched from storage, so it exists; qed.");
			}
		}
```

**File:** substrate/frame/staking-async/src/pallet/mod.rs (L2446-2469)
```rust
		/// Pay out next page of the stakers behind a validator for the given era.
		///
		/// - `validator_stash` is the stash account of the validator.
		/// - `era` may be any era between `[current_era - history_depth; current_era]`.
		///
		/// The origin of this call must be _Signed_. Any account can call this function, even if
		/// it is not one of the stakers.
		///
		/// The reward payout could be paged in case there are too many nominators backing the
		/// `validator_stash`. This call will payout unpaid pages in an ascending order. To claim a
		/// specific page, use `payout_stakers_by_page`.`
		///
		/// If all pages are claimed, it returns an error `InvalidPage`.
		#[pallet::call_index(18)]
		#[pallet::weight(T::WeightInfo::payout_stakers_alive_staked(T::MaxExposurePageSize::get()))]
		pub fn payout_stakers(
			origin: OriginFor<T>,
			validator_stash: T::AccountId,
			era: EraIndex,
		) -> DispatchResultWithPostInfo {
			ensure_signed(origin)?;

			Self::do_payout_stakers(validator_stash, era)
		}
```

**File:** substrate/frame/staking/src/pallet/impls.rs (L176-177)
```rust
		ledger.total = ledger.total.checked_add(&extra).ok_or(ArithmeticError::Overflow)?;
		ledger.active = ledger.active.checked_add(&extra).ok_or(ArithmeticError::Overflow)?;
```

**File:** prdoc/1.5.0/pr_2435_special.prdoc (L1-9)
```text
title: "pallet-staking: Converts all math operations to safe"
author: gpestanaar
topic: Pallets

doc:
  - audience: Runtime Dev
    description: n/a

crates: []
```

**File:** prdoc/stable2509/pr_9186.prdoc (L1-9)
```text
title: 'pallet-staking-async: Use saturating addition for era reward points'
doc:
- audience: Runtime Dev
  description: |-
    This PR replaces regular addition with saturating addition when accumulating era reward points in
    pallet-staking-async to prevent potential overflow.
crates:
- name: pallet-staking-async
  bump: patch
```

**File:** substrate/frame/staking-async/src/ledger.rs (L83-100)
```rust
pub struct StakingLedger<T: Config> {
	/// The stash account whose balance is actually locked and at stake.
	pub stash: T::AccountId,

	/// The total amount of the stash's balance that we are currently accounting for.
	/// It's just `active` plus all the `unlocking` balances.
	#[codec(compact)]
	pub total: BalanceOf<T>,

	/// The total amount of the stash's balance that will be at stake in any forthcoming
	/// rounds.
	#[codec(compact)]
	pub active: BalanceOf<T>,

	/// Any balance that is becoming free, which may eventually be transferred out of the stash
	/// (assuming it doesn't get slashed first). It is assumed that this will be treated as a first
	/// in, first out queue where the new (higher value) eras get pushed on the back.
	pub unlocking: BoundedVec<UnlockChunk<BalanceOf<T>>, T::MaxUnlockingChunks>,
```
