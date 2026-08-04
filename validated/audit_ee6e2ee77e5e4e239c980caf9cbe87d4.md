### Title
Unbounded-lifetime vesting schedule spam permanently occupies a victim's `MaxVestingSchedules` slots via public `vested_transfer` - (File: `substrate/frame/vesting/src/lib.rs`)

### Summary
`pallet_vesting::vested_transfer` and `force_vested_transfer` let *any* signed account push a new `VestingInfo` schedule onto an *arbitrary target's* `Vesting` storage entry, which is a `BoundedVec` capped at `MAX_VESTING_SCHEDULES` slots [1](#0-0) . Because `per_block` is only required to be non-zero (`is_valid` checks `!locked.is_zero() && !raw_per_block().is_zero()`) and the schedule duration is computed as `locked / per_block()` [2](#0-1) , an attacker can create a schedule with `locked = MinVestedTransfer` (the minimum allowed) and `per_block = 1` (the minimum non-zero value), producing a vesting duration of `MinVestedTransfer` blocks — effectively centuries or millennia depending on chain token decimals. Repeating this against the same target account, this fills up all `MAX_VESTING_SCHEDULES` array slots with near-eternal, near-worthless schedules that the target never asked for and cannot decline, permanently denying that account any further legitimate `vested_transfer`s (payroll, grants, treasury vesting, etc.) with `Error::AtMaxVestingSchedules`.

### Finding Description
`add_vesting_schedule` unconditionally `try_push`es a new schedule into the target's `Vesting<T>` `BoundedVec` and only fails once the vec is at capacity: [3](#0-2) 

`do_vested_transfer` (invoked by the public, unprivileged extrinsic `vested_transfer`) only checks `schedule.locked() >= T::MinVestedTransfer::get()` and `schedule.is_valid()` before calling `add_vesting_schedule` on the *target*, with no consent or filtering from the target: [4](#0-3) 

`is_valid()` only rejects `locked == 0` or `per_block == 0`; it places no floor on the *ratio* between them, so an attacker can pick `locked = MinVestedTransfer` (satisfying the enforced minimum) and `per_block = 1` (the smallest legal value) to obtain `ending_block = starting_block + locked/per_block`, i.e. a duration measured in `MinVestedTransfer` blocks: [5](#0-4) 

Each account's vesting schedules are stored in a single `BoundedVec<VestingInfo<...>, MaxVestingSchedulesGet<T>>`, so once `MAX_VESTING_SCHEDULES` entries exist, any further `add_vesting_schedule` call (from anyone, including legitimate senders) returns `Error::AtMaxVestingSchedules`: [6](#0-5) [1](#0-0) 

Unlike the RealWagmi bug, this is not primarily a gas-griefing vector on other users' transactions — `vest`/`vest_other`/`merge_schedules` weights already account for `MAX_VESTING_SCHEDULES` worst case, so it does not out-of-gas revert unrelated calls. The real broken invariant is **capacity griefing / permanent DoS of a fixed-size, permissionless-writable array belonging to a victim account**: the target has no way to opt out of receiving spam schedules, and `merge_schedules` cannot rescue the slot economically because merging two schedules keeps `ending_block = max(schedule1_end, schedule2_end)` [7](#0-6)  — merging a spam eternal-duration schedule with a real one just makes the *real* schedule inherit the eternal duration and does not remove the occupied array slot's associated lock burden. The `Passive`/filtering path in `report_schedule_updates` only drops schedules whose `locked_at(now)` reaches zero [8](#0-7) , which for an attacker-chosen multi-millennial duration schedule never happens within any realistic operational timeframe.

### Impact Explanation
An unprivileged attacker can permanently occupy every one of a victim account's `MaxVestingSchedules` slots for a bounded, one-time cost of `MAX_VESTING_SCHEDULES * MinVestedTransfer` tokens (paid to the victim, not burned, but locked from ordinary transferability for the schedule's duration, which the attacker deliberately sets to be effectively infinite). Once full, the victim can never again receive a `vested_transfer` (nor can `force_vested_transfer` from root succeed, since it hits the same `AtMaxVestingSchedules` capacity check) — a real, on-chain, permanent denial of a documented and intended pallet function against arbitrary target accounts (e.g. new employees, grant recipients, parachain treasuries configured to receive vesting distributions). This fits the "public underpriced work" / "permanent user-fund or bridge-state lock"-style impact class: a cheap, permissionless action locks legitimate future economic settlement (vesting-based payouts) for a target account indefinitely.

### Likelihood Explanation
High: `vested_transfer` is a standard signed extrinsic requiring only the `MinVestedTransfer` balance and no interaction/consent from the target. `MinVestedTransfer` is typically set to a small, fixed existential-style amount in production runtimes, so the total cost to fill all `MAX_VESTING_SCHEDULES` slots against any target address is modest and entirely attacker-controlled (no race with other actors, no governance/admin dependency, no malicious peer/validator required).

### Recommendation
- Disallow (or require target opt-in / origin match) `vested_transfer` schedules whose duration `locked/per_block` exceeds a sane bound (e.g., cap max schedule duration relative to `BlockNumberToBalance` scale), so `per_block=1` cannot be combined with a large `locked` amount to produce unbounded duration.
- Alternatively, allow the target (or a filtered set of trusted senders) to reject/cancel unwanted incoming vesting schedules, or make schedule slot consumption require target consent for third-party-initiated transfers.
- Enforce a minimum `per_block` relative to `locked` (e.g., `per_block >= locked / MAX_REASONABLE_DURATION`) in `VestingInfo::is_valid`.

### Proof of Concept
1. Attacker `A` holds `MAX_VESTING_SCHEDULES * MinVestedTransfer` tokens (or repeats over time).
2. For `i in 0..MAX_VESTING_SCHEDULES`, `A` calls `Vesting::vested_transfer(target, VestingInfo::new(MinVestedTransfer, 1, current_block))` — each call passes `is_valid()` (`locked != 0`, `per_block != 0`) and the `locked >= MinVestedTransfer` check in `do_vested_transfer`.
3. Each call succeeds via `add_vesting_schedule`'s `try_push` until the target's `Vesting` `BoundedVec` reaches `MAX_VESTING_SCHEDULES`; the final legitimate attempt (by anyone, including `force_vested_transfer` from root) fails with `Error::AtMaxVestingSchedules`.
4. Because `ending_block = starting_block + locked/per_block = starting_block + MinVestedTransfer` (in blocks), and `MinVestedTransfer` is denominated in the chain's smallest currency unit (a large integer under normal decimals configuration), the schedules remain "active" (`locked_at(now) != 0`) for a duration vastly exceeding any realistic chain lifetime, permanently denying the target further vesting transfers.

### Citations

**File:** substrate/frame/vesting/src/lib.rs (L146-152)
```rust
// Wrapper for `T::MAX_VESTING_SCHEDULES` to satisfy `trait Get`.
pub struct MaxVestingSchedulesGet<T>(PhantomData<T>);
impl<T: Config> Get<u32> for MaxVestingSchedulesGet<T> {
	fn get() -> u32 {
		T::MAX_VESTING_SCHEDULES
	}
}
```

**File:** substrate/frame/vesting/src/lib.rs (L503-550)
```rust
	// Create a new `VestingInfo`, based off of two other `VestingInfo`s.
	// NOTE: We assume both schedules have had funds unlocked up through the current block.
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

**File:** substrate/frame/vesting/src/lib.rs (L588-618)
```rust
	/// Iterate through the schedules to track the current locked amount and
	/// filter out completed and specified schedules.
	///
	/// Returns a tuple that consists of:
	/// - Vec of vesting schedules, where completed schedules and those specified
	/// 	by filter are removed. (Note the vec is not checked for respecting
	/// 	bounded length.)
	/// - The amount locked at the current block number based on the given schedules.
	///
	/// NOTE: the amount locked does not include any schedules that are filtered out via `action`.
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

**File:** substrate/frame/vesting/src/lib.rs (L780-816)
```rust
	fn add_vesting_schedule(
		who: &T::AccountId,
		locked: BalanceOf<T>,
		per_block: BalanceOf<T>,
		starting_block: BlockNumberFor<T>,
	) -> DispatchResult {
		if locked.is_zero() {
			return Ok(());
		}

		let vesting_schedule = VestingInfo::new(locked, per_block, starting_block);
		// Check for `per_block` or `locked` of 0.
		if !vesting_schedule.is_valid() {
			return Err(Error::<T>::InvalidScheduleParams.into());
		};

		let mut schedules = Vesting::<T>::get(who).unwrap_or_default();

		// NOTE: we must push the new schedule so that `exec_action`
		// will give the correct new locked amount.
		ensure!(schedules.try_push(vesting_schedule).is_ok(), Error::<T>::AtMaxVestingSchedules);

		debug_assert!(schedules.len() > 0, "schedules cannot be empty after insertion");
		let schedule_index = schedules.len() - 1;
		Self::deposit_event(Event::<T>::VestingCreated {
			account: who.clone(),
			schedule_index: schedule_index as u32,
		});

		let (schedules, locked_now) =
			Self::exec_action(schedules.to_vec(), VestingAction::Passive)?;

		Self::write_vesting(who, schedules)?;
		Self::write_lock(who, locked_now);

		Ok(())
	}
```

**File:** substrate/frame/vesting/src/vesting_info.rs (L58-124)
```rust
	/// Validate parameters for `VestingInfo`. Note that this does not check
	/// against `MinVestedTransfer`.
	pub fn is_valid(&self) -> bool {
		!self.locked.is_zero() && !self.raw_per_block().is_zero()
	}

	/// Locked amount at schedule creation.
	pub fn locked(&self) -> Balance {
		self.locked
	}

	/// Amount that gets unlocked every block after `starting_block`. Corrects for `per_block` of 0.
	/// We don't let `per_block` be less than 1, or else the vesting will never end.
	/// This should be used whenever accessing `per_block` unless explicitly checking for 0 values.
	pub fn per_block(&self) -> Balance {
		self.per_block.max(One::one())
	}

	/// Get the unmodified `per_block`. Generally should not be used, but is useful for
	/// validating `per_block`.
	pub(crate) fn raw_per_block(&self) -> Balance {
		self.per_block
	}

	/// Starting block for unlocking(vesting).
	pub fn starting_block(&self) -> BlockNumber {
		self.starting_block
	}

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

	/// Block number at which the schedule ends (as type `Balance`).
	pub fn ending_block_as_balance<BlockNumberToBalance: Convert<BlockNumber, Balance>>(
		&self,
	) -> Balance {
		let starting_block = BlockNumberToBalance::convert(self.starting_block);
		let duration = if self.per_block() >= self.locked {
			// If `per_block` is bigger than `locked`, the schedule will end
			// the block after starting.
			One::one()
		} else {
			self.locked / self.per_block() +
				if (self.locked % self.per_block()).is_zero() {
					Zero::zero()
				} else {
					// `per_block` does not perfectly divide `locked`, so we need an extra block to
					// unlock some amount less than `per_block`.
					One::one()
				}
		};

		starting_block.saturating_add(duration)
	}
```
