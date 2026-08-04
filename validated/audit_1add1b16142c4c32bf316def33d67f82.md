No vulnerability found for this question.

**Analysis:** `merge_schedules` executes entirely within a single atomic extrinsic call — it reads the account's `Vesting` schedules, computes the merge via `exec_action`/`merge_vesting_info`, and writes both the updated schedule list and lock via `write_vesting` and `write_lock` before returning [1](#0-0) . There is no multi-block or multi-call "cleanup" step that leaves an externally observable, exploitable intermediate storage state between a precursor and follow-up call — the storage mutation is committed as a whole (or rolled back on error) within the single dispatch, since Substrate extrinsics are atomic: any error prior to `write_vesting`/`write_lock` aborts before storage is touched, and both writes happen unconditionally together at the end of the call.

The merge logic itself preserves the locked-balance invariant: `report_schedule_updates` sums `locked_at` for all retained schedules, and `merge_vesting_info` correctly folds the two merged schedules' locked amounts into the new schedule or falls back to keeping one unmodified schedule if the other has ended [2](#0-1) [3](#0-2) . The final `write_lock` call sets the on-chain lock to exactly `locked_now`, which is always consistent with the schedules just written [4](#0-3) .

Because there is no reachable intermediate state between two public calls (the entire merge happens inside one dispatch, and successive extrinsics in a block only see the fully-updated post-state of the prior one), the "precursor call → merge_schedules → follow-up call" exploitation pattern described in the question does not apply here. No unauthorized execution, wrongful asset movement, or duplicate settlement path exists in this code.

### Citations

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

**File:** substrate/frame/vesting/src/lib.rs (L620-633)
```rust
	/// Write an accounts updated vesting lock to storage.
	fn write_lock(who: &T::AccountId, total_locked_now: BalanceOf<T>) {
		if total_locked_now.is_zero() {
			T::Currency::remove_lock(VESTING_ID, who);
			Self::deposit_event(Event::<T>::VestingCompleted { account: who.clone() });
		} else {
			let reasons = WithdrawReasons::except(T::UnvestedFundsAllowedWithdrawReasons::get());
			T::Currency::set_lock(VESTING_ID, who, total_locked_now, reasons);
			Self::deposit_event(Event::<T>::VestingUpdated {
				account: who.clone(),
				unvested: total_locked_now,
			});
		};
	}
```

**File:** substrate/frame/vesting/src/lib.rs (L669-709)
```rust
	fn exec_action(
		schedules: Vec<VestingInfo<BalanceOf<T>, BlockNumberFor<T>>>,
		action: VestingAction,
	) -> Result<(Vec<VestingInfo<BalanceOf<T>, BlockNumberFor<T>>>, BalanceOf<T>), DispatchError> {
		let (schedules, locked_now) = match action {
			VestingAction::Merge { index1: idx1, index2: idx2 } => {
				// The schedule index is based off of the schedule ordering prior to filtering out
				// any schedules that may be ending at this block.
				let schedule1 = *schedules.get(idx1).ok_or(Error::<T>::ScheduleIndexOutOfBounds)?;
				let schedule2 = *schedules.get(idx2).ok_or(Error::<T>::ScheduleIndexOutOfBounds)?;

				// The length of `schedules` decreases by 2 here since we filter out 2 schedules.
				// Thus we know below that we can push the new merged schedule without error
				// (assuming initial state was valid).
				let (mut schedules, mut locked_now) =
					Self::report_schedule_updates(schedules.to_vec(), action);

				let now = T::BlockNumberProvider::current_block_number();
				if let Some(new_schedule) = Self::merge_vesting_info(now, schedule1, schedule2) {
					// Merging created a new schedule so we:
					// 1) need to add it to the accounts vesting schedule collection,
					schedules.push(new_schedule);
					// (we use `locked_at` in case this is a schedule that started in the past)
					let new_schedule_locked =
						new_schedule.locked_at::<T::BlockNumberToBalance>(now);
					// and 2) update the locked amount to reflect the schedule we just added.
					locked_now = locked_now.saturating_add(new_schedule_locked);
				} // In the None case there was no new schedule to account for.

				(schedules, locked_now)
			},
			_ => Self::report_schedule_updates(schedules.to_vec(), action),
		};

		debug_assert!(
			locked_now > Zero::zero() && schedules.len() > 0 ||
				locked_now == Zero::zero() && schedules.len() == 0
		);

		Ok((schedules, locked_now))
	}
```
