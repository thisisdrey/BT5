### Title
Missing upper bound on `pallet-vesting` schedule duration allows unbounded-length vesting locks and `MaxVestingSchedules` griefing DoS - (File: `substrate/frame/vesting/src/vesting_info.rs`, `substrate/frame/vesting/src/lib.rs`)

### Summary
`VestingInfo::is_valid()` in `pallet-vesting` only checks that `locked` and `per_block` are non-zero; it never bounds the resulting vesting *duration* (`locked / per_block`). The public, unprivileged `vested_transfer` extrinsic accepts an attacker-supplied `VestingInfo{locked, per_block, starting_block}` and forwards it directly to `do_vested_transfer`, which only enforces a *minimum* amount (`MinVestedTransfer`) — never a maximum duration. This mirrors the external report's "missing upper bound" pattern, except here the unbounded value is reachable by any signed account, not the owner, satisfying the unprivileged-attacker requirement. [1](#0-0) [2](#0-1) [3](#0-2) 

### Finding Description
`is_valid()` only guards against zero values:
```
pub fn is_valid(&self) -> bool {
    !self.locked.is_zero() && !self.raw_per_block().is_zero()
}
``` [1](#0-0) 

`do_vested_transfer` only checks `schedule.locked() >= T::MinVestedTransfer::get()` and `schedule.is_valid()` before transferring funds and creating the schedule — there is no check on `ending_block_as_balance()` or on `locked / per_block`: [4](#0-3) 

The `vested_transfer` dispatchable is callable by any signed account against any `target`, with a fully attacker-controlled `VestingInfo`: [5](#0-4) 

An attacker can set `locked = MinVestedTransfer` (the minimum allowed) and `per_block = 1`, producing `ending_block_as_balance = starting_block + locked`, i.e., a vesting duration proportional to the full `Balance` range — effectively unbounded/near-permanent, exactly like an unbounded `vestPeriod`. This creates a schedule entry in the target's `Vesting` storage that will never (practically) finish vesting.

Two compounding effects turn this from a self-only griefing spend into a real DoS on the *victim* account:
1. Each call consumes one of the victim's fixed `MaxVestingSchedules` slots (`AtMaxVestingSchedules` bound enforced via `try_push`): [6](#0-5) 
By repeatedly calling `vested_transfer` with the minimum allowed amount and maximal duration, an attacker can fill all of a victim's vesting slots with schedules that never complete, blocking any future legitimate vested transfer (e.g., from an employer or grant program) to that account until the victim manually merges/removes schedules.

2. `merge_schedules`, the only user-controlled remediation, takes the **maximum** of the two schedules' ending blocks when merging:
```
let ending_block = schedule1_ending_block.max(schedule2_ending_block);
``` [7](#0-6) 
If the victim (or any tooling acting on their behalf) merges a legitimate schedule with one of the attacker's poisoned near-infinite-duration schedules to free up a slot, the resulting merged schedule inherits the attacker's near-infinite `ending_block`, permanently extending the lock duration of the victim's real vested funds as well.

Existing guards do not stop this path: `MinVestedTransfer` only bounds the amount from below, `MaxVestingSchedules` bounds the *count* of schedules (which the attacker can still exhaust), and `is_valid()` bounds `locked`/`per_block` only away from zero — none of these bound the vesting *duration*.

### Impact Explanation
This is a public, underpriced-work / permanent-fund-lock class issue reachable by any unprivileged signed account against an arbitrary victim account: it can permanently lock a portion of the victim's balance under the `vesting` `LockIdentifier`, and can exhaust the victim's bounded vesting-schedule slots, denying them the ability to receive legitimate vesting transfers indefinitely. If a legitimate schedule is later merged with a poisoned one (a natural remediation step), the victim's real vested funds inherit the unbounded duration too, converting a griefing schedule into a de-facto permanent lock on real funds.

### Likelihood Explanation
Likelihood is high: `vested_transfer` is a normal, permissionless extrinsic with no rate limiting beyond the `MinVestedTransfer` floor and normal transaction fees; an attacker only needs enough balance to cover `MinVestedTransfer` (which can be a small configured constant) times the number of `MaxVestingSchedules` slots to fully saturate a target account.

### Recommendation
Add an explicit maximum-duration (or maximum `ending_block`/minimum `per_block` relative to `locked`) check in `VestingInfo::is_valid()` or in `do_vested_transfer`, analogous to bounding `vestPeriod` in the original report, e.g. reject schedules whose `ending_block_as_balance() - starting_block` exceeds a configurable `MaxVestingDuration`. Additionally, consider requiring recipient consent (or a minimum `per_block` floor relative to `locked`) for third-party `vested_transfer` calls, and ensure `merge_schedules` cannot silently inherit an attacker-poisoned schedule's near-infinite `ending_block` without the recipient's explicit action or bound checking.

### Proof of Concept
1. Attacker holds balance `≥ MinVestedTransfer` (call it `L`), targets victim account `V`.
2. Attacker calls `vested_transfer(target = V, schedule = VestingInfo::new(locked = L, per_block = 1, starting_block = current_block))`.
   - `is_valid()` passes (`L != 0`, `per_block = 1 != 0`).
   - `do_vested_transfer` only checks `L >= MinVestedTransfer` — passes.
   - Resulting `ending_block_as_balance = starting_block + L`, i.e., duration ≈ `L` blocks (can be made arbitrarily large by using a `Balance` type with large range, up to `Balance::max_value()` if the attacker can afford it, or simply large enough to exceed any realistic chain lifetime even at `MinVestedTransfer`).
3. Repeat step 2 up to `MaxVestingSchedules` times (using new minimal-amount transfers) to fully occupy `V`'s `Vesting` storage entry, each schedule effectively permanent.
4. Any subsequent legitimate `vested_transfer`/`force_vested_transfer` to `V` fails with `Error::AtMaxVestingSchedules` [8](#0-7)  until `V` calls `merge_schedules`, which — per the `max()` logic in `merge_vesting_info` — propagates the attacker's near-infinite `ending_block` into the merged, otherwise-legitimate schedule. [9](#0-8)

### Citations

**File:** substrate/frame/vesting/src/vesting_info.rs (L58-62)
```rust
	/// Validate parameters for `VestingInfo`. Note that this does not check
	/// against `MinVestedTransfer`.
	pub fn is_valid(&self) -> bool {
		!self.locked.is_zero() && !self.raw_per_block().is_zero()
	}
```

**File:** substrate/frame/vesting/src/lib.rs (L303-305)
```rust
		/// The account already has `MaxVestingSchedules` count of schedules and thus
		/// cannot add another one. Consider merging existing schedules in order to add another.
		AtMaxVestingSchedules,
```

**File:** substrate/frame/vesting/src/lib.rs (L355-380)
```rust
		/// Create a vested transfer.
		///
		/// The dispatch origin for this call must be _Signed_.
		///
		/// - `target`: The account receiving the vested funds.
		/// - `schedule`: The vesting schedule attached to the transfer.
		///
		/// Emits `VestingCreated`.
		///
		/// NOTE: This will unlock all schedules through the current block.
		///
		/// ## Complexity
		/// - `O(1)`.
		#[pallet::call_index(2)]
		#[pallet::weight(
			T::WeightInfo::vested_transfer(MaxLocksOf::<T>::get(), T::MAX_VESTING_SCHEDULES)
		)]
		pub fn vested_transfer(
			origin: OriginFor<T>,
			target: AccountIdLookupOf<T>,
			schedule: VestingInfo<BalanceOf<T>, BlockNumberFor<T>>,
		) -> DispatchResult {
			let transactor = ensure_signed(origin)?;
			let target = T::Lookup::lookup(target)?;
			Self::do_vested_transfer(&transactor, &target, schedule)
		}
```

**File:** substrate/frame/vesting/src/lib.rs (L438-460)
```rust
		pub fn merge_schedules(
			origin: OriginFor<T>,
			schedule1_index: u32,
			schedule2_index: u32,
		) -> DispatchResult {
			let who = ensure_signed(origin)?;
			if schedule1_index == schedule2_index {
				return Ok(());
			};
			let schedule1_index = schedule1_index as usize;
			let schedule2_index = schedule2_index as usize;

			let schedules = Vesting::<T>::get(&who).ok_or(Error::<T>::NotVesting)?;
			let merge_action =
				VestingAction::Merge { index1: schedule1_index, index2: schedule2_index };

			let (schedules, locked_now) = Self::exec_action(schedules.to_vec(), merge_action)?;

			Self::write_vesting(&who, schedules)?;
			Self::write_lock(&who, locked_now);

			Ok(())
		}
```

**File:** substrate/frame/vesting/src/lib.rs (L505-550)
```rust
	fn merge_vesting_info(
		now: BlockNumberFor<T>,
		schedule1: VestingInfo<BalanceOf<T>, BlockNumberFor<T>>,
		schedule2: VestingInfo<BalanceOf<T>, BlockNumberFor<T>>,
	) -> Option<VestingInfo<BalanceOf<T>, BlockNumberFor<T>>> {
		let schedule1_ending_block = schedule1.ending_block_as_balance::<T::BlockNumberToBalance>();
		let schedule2_ending_block = schedule2.ending_block_as_balance::<T::BlockNumberToBalance>();
		let now_as_balance = T::BlockNumberToBalance::convert(now);

		// Check if one or both schedules have ended.
		match (schedule1_ending_block <= now_as_balance, schedule2_ending_block <= now_as_balance) {
			// If both schedules have ended, we don't merge and exit early.
			(true, true) => return None,
			// If one schedule has ended, we treat the one that has not ended as the new
			// merged schedule.
			(true, false) => return Some(schedule2),
			(false, true) => return Some(schedule1),
			// If neither schedule has ended don't exit early.
			_ => {},
		}

		let locked = schedule1
			.locked_at::<T::BlockNumberToBalance>(now)
			.saturating_add(schedule2.locked_at::<T::BlockNumberToBalance>(now));
		// This shouldn't happen because we know at least one ending block is greater than now,
		// thus at least a schedule a some locked balance.
		debug_assert!(
			!locked.is_zero(),
			"merge_vesting_info validation checks failed to catch a locked of 0"
		);

		let ending_block = schedule1_ending_block.max(schedule2_ending_block);
		let starting_block = now.max(schedule1.starting_block()).max(schedule2.starting_block());

		let per_block = {
			let duration = ending_block
				.saturating_sub(T::BlockNumberToBalance::convert(starting_block))
				.max(One::one());
			(locked / duration).max(One::one())
		};

		let schedule = VestingInfo::new(locked, per_block, starting_block);
		debug_assert!(schedule.is_valid(), "merge_vesting_info schedule validation check failed");

		Some(schedule)
	}
```

**File:** substrate/frame/vesting/src/lib.rs (L552-586)
```rust
	// Execute a vested transfer from `source` to `target` with the given `schedule`.
	fn do_vested_transfer(
		source: &T::AccountId,
		target: &T::AccountId,
		schedule: VestingInfo<BalanceOf<T>, BlockNumberFor<T>>,
	) -> DispatchResult {
		// Validate user inputs.
		ensure!(schedule.locked() >= T::MinVestedTransfer::get(), Error::<T>::AmountLow);
		if !schedule.is_valid() {
			return Err(Error::<T>::InvalidScheduleParams.into());
		};

		// Check we can add to this account prior to any storage writes.
		Self::can_add_vesting_schedule(
			target,
			schedule.locked(),
			schedule.per_block(),
			schedule.starting_block(),
		)?;

		T::Currency::transfer(source, target, schedule.locked(), ExistenceRequirement::AllowDeath)?;

		// We can't let this fail because the currency transfer has already happened.
		// Must be successful as it has been checked before.
		// Better to return error on failure anyway.
		let res = Self::add_vesting_schedule(
			target,
			schedule.locked(),
			schedule.per_block(),
			schedule.starting_block(),
		);
		debug_assert!(res.is_ok(), "Failed to add a schedule when we had to succeed.");

		Ok(())
	}
```

**File:** substrate/frame/vesting/src/lib.rs (L796-800)
```rust
		let mut schedules = Vesting::<T>::get(who).unwrap_or_default();

		// NOTE: we must push the new schedule so that `exec_action`
		// will give the correct new locked amount.
		ensure!(schedules.try_push(vesting_schedule).is_ok(), Error::<T>::AtMaxVestingSchedules);
```
