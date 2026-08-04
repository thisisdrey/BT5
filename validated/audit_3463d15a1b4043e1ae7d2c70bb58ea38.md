## Finding: `pallet_vesting::vested_transfer` griefing via unauthorized third-party schedule injection

This is a solid local analog of the reported bug class: a public, permissionless entrypoint (`vested_transfer`) lets an attacker act on behalf of an arbitrary victim account and push entries into that victim's fixed-capacity bounded structure (`Vesting<T>` `BoundedVec`, capped at `MAX_VESTING_SCHEDULES`), exactly like the Fenwick tree's `maxDepositEntries` griefing.

### Title
Permissionless `vested_transfer` allows griefing any account's bounded vesting-schedule slots — `substrate/frame/vesting/src/lib.rs`

### Summary
`pallet_vesting::vested_transfer` performs no check that `msg.sender`/origin has any relationship to the `target` account. Any signed account can repeatedly call it with the minimum allowed amount (`MinVestedTransfer`) against an arbitrary victim, filling the victim's `Vesting<T>` `BoundedVec` up to `MAX_VESTING_SCHEDULES`, exactly mirroring the reported `addLiquidity()`/Fenwick-tree griefing where an unchecked caller fills a victim's bounded storage on their behalf.

### Finding Description
`vested_transfer` is dispatchable by any signed origin and takes an arbitrary `target` lookup with no relation check to the caller: [1](#0-0) 

It calls `do_vested_transfer`, which validates the schedule and then unconditionally pushes a new `VestingInfo` entry into the target's bounded vesting list via `add_vesting_schedule`: [2](#0-1) 

`add_vesting_schedule` enforces the capacity bound but does not check the caller/target relationship — it only checks `MAX_VESTING_SCHEDULES`: [3](#0-2) 

The storage itself is a `BoundedVec` capped by `MAX_VESTING_SCHEDULES`, identical in role to the Fenwick tree's `maxDepositEntries`: [4](#0-3) 

Tests confirm this is the exact boundary condition — once `MAX_VESTING_SCHEDULES` entries exist for an account, any further `vested_transfer` to that account fails with `AtMaxVestingSchedules`, regardless of who is sending or how legitimate the intent: [5](#0-4) 

The only self-service remedy is `merge_schedules`, callable only by the victim (`ensure_signed` must equal `who` owning the schedules), which can compact two schedules into one and free a slot even before either fully vests: [6](#0-5) 

However this requires the victim to actively notice and repeatedly spend transaction fees to counter an attacker who can keep re-filling the last free slot with a fresh dust deposit (paying only `MinVestedTransfer` each time, funds which the attacker permanently loses to the victim but which nonetheless make the victim's account continuously unable to receive a *new, distinct* scheduled transfer from a third party without the victim first taking action).

### Impact Explanation
An attacker can block any account from receiving further `vested_transfer`/`force_vested_transfer`-style scheduled payouts (e.g., salary vesting, crowdloan/rewards distribution flows built on this pallet, or the `VestedPayout`/`VestedTransfer` trait consumers) by pre-filling their `MAX_VESTING_SCHEDULES` slots. This is a public-entrypoint denial-of-service against a specific account's ability to receive vesting-based settlements, matching the "public underpriced work" / "duplicate settlement or payout" disruption category, though bounded in severity because the attacker's own funds are irreversibly transferred (locked) into the victim rather than recoverable, and the victim has a self-service `merge_schedules` remedy.

### Likelihood Explanation
Medium: the attack is simple and requires no privileged role, node, or validator — just a signed account with enough balance to cover `MinVestedTransfer * MAX_VESTING_SCHEDULES`. It is entirely permissionless, matching the report's "no `msg.sender` check" root cause. Likelihood is tempered by the ongoing cost to the attacker (real, non-recoverable fund transfers) and by the existence of `merge_schedules` as a counter, unlike the original Fenwick tree bug which had no consolidation mechanism short of waiting out the full lock duration.

### Recommendation
- Consider restricting `vested_transfer` third-party griefing surface, e.g., by requiring recipient opt-in/consent for schedules initiated by non-root/non-self origins, or by automatically merging/compacting a new incoming schedule with an existing dust schedule when capacity is exhausted rather than hard-failing.
- Alternatively, expose a permissionless "auto-merge on overflow" path so a `vested_transfer` that would exceed `MAX_VESTING_SCHEDULES` merges with the oldest/smallest existing schedule instead of reverting, closing the DoS window without requiring victim intervention.

### Proof of Concept
1. Attacker funds an account with `MinVestedTransfer * MAX_VESTING_SCHEDULES` tokens.
2. Attacker calls `Vesting::vested_transfer(origin=attacker, target=victim, schedule=minimal_valid_schedule)` `MAX_VESTING_SCHEDULES` times in a row, each succeeding per `add_vesting_schedule`'s only guard being the capacity check.
3. Any subsequent legitimate `vested_transfer` (e.g., from an employer, crowdloan reward payer, or another protocol integrating `VestedTransfer`) to `victim` now fails with `Error::AtMaxVestingSchedules`, as shown by the existing test `vested_transfer_allows_max_schedules`: [7](#0-6) 
4. `victim` must proactively call `merge_schedules` and pay transaction fees to reclaim slots, while attacker can immediately re-inject a new dust schedule to reoccupy the freed slot, sustaining the block as long as they're willing to keep transferring small amounts.

### Citations

**File:** substrate/frame/vesting/src/lib.rs (L207-233)
```rust
		/// Maximum number of vesting schedules an account may have at a given moment.
		const MAX_VESTING_SCHEDULES: u32;
	}

	#[pallet::extra_constants]
	impl<T: Config> Pallet<T> {
		#[pallet::constant_name(MaxVestingSchedules)]
		fn max_vesting_schedules() -> u32 {
			T::MAX_VESTING_SCHEDULES
		}
	}

	#[pallet::hooks]
	impl<T: Config> Hooks<BlockNumberFor<T>> for Pallet<T> {
		fn integrity_test() {
			assert!(T::MAX_VESTING_SCHEDULES > 0, "`MaxVestingSchedules` must be greater than 0");
		}
	}

	/// Information regarding the vesting of a given account.
	#[pallet::storage]
	pub type Vesting<T: Config> = StorageMap<
		_,
		Blake2_128Concat,
		T::AccountId,
		BoundedVec<VestingInfo<BalanceOf<T>, BlockNumberFor<T>>, MaxVestingSchedulesGet<T>>,
	>;
```

**File:** substrate/frame/vesting/src/lib.rs (L372-380)
```rust
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

**File:** substrate/frame/vesting/src/tests.rs (L415-443)
```rust
#[test]
fn vested_transfer_allows_max_schedules() {
	ExtBuilder::default().existential_deposit(ED).build().execute_with(|| {
		let mut user_4_free_balance = Balances::free_balance(&4);
		let max_schedules = <Test as Config>::MAX_VESTING_SCHEDULES;
		let sched = VestingInfo::new(
			<Test as Config>::MinVestedTransfer::get(),
			1, // Vest over 2 * 256 blocks.
			10,
		);

		// Add max amount schedules to user 4.
		for _ in 0..max_schedules {
			assert_ok!(Vesting::vested_transfer(Some(13).into(), 4, sched));
		}

		// The schedules count towards vesting balance
		let transferred_amount = <Test as Config>::MinVestedTransfer::get() * max_schedules as u64;
		assert_eq!(Vesting::vesting_balance(&4), Some(transferred_amount));
		// and free balance.
		user_4_free_balance += transferred_amount;
		assert_eq!(Balances::free_balance(&4), user_4_free_balance);

		// Cannot insert a 4th vesting schedule when `MaxVestingSchedules` === 3,
		assert_noop!(
			Vesting::vested_transfer(Some(3).into(), 4, sched),
			Error::<Test>::AtMaxVestingSchedules,
		);
		// so the free balance does not change.
```
