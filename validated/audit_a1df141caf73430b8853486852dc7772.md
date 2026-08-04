### Title
Unprivileged callers can permanently exhaust a target account's `MAX_VESTING_SCHEDULES` cap in `pallet-vesting`, blocking all future legitimate vested transfers to that account - (File: `substrate/frame/vesting/src/lib.rs`)

### Summary
`pallet-vesting::vested_transfer` lets *any* signed account push a new vesting schedule into an arbitrary `target` account's storage, bounded by the fixed constant `MAX_VESTING_SCHEDULES`. Because the only gate on this write is `MinVestedTransfer` (a per-transfer minimum, not a per-account cap on number of griefers), an attacker can repeatedly call `vested_transfer` from many different funded accounts (or repeatedly, once each schedule they created has fully vested and been cleaned up they can immediately redo it) sending the minimum amount to fill up the victim's `Vesting<T>` `BoundedVec` up to `MAX_VESTING_SCHEDULES`. Once full, any other party's legitimate `vested_transfer`/`force_vested_transfer` to that same victim fails with `Error::AtMaxVestingSchedules`, exactly mirroring the veALCX bug where filling the `MAX_DELEGATES` limit for a victim's delegate list blocked all future legitimate delegations to that victim.

### Finding Description
`Vesting<T>` is a `StorageMap<AccountId, BoundedVec<VestingInfo<...>, MaxVestingSchedulesGet<T>>>` keyed by the **recipient**, not the caller: [1](#0-0) 

`vested_transfer` is callable by any signed origin and lets the caller choose an arbitrary `target`: [2](#0-1) 

The write path, `do_vested_transfer` → `add_vesting_schedule`, only checks that the transferred amount is not below `MinVestedTransfer` and that the target's schedule count is below `MAX_VESTING_SCHEDULES`; it does **not** check who the previous schedules came from or apply any per-caller/per-source rate limiting on a given target: [3](#0-2) [4](#0-3) 

The guard that ultimately blocks new schedules is a hard, target-scoped cap enforced via `BoundedVec::try_push`/`try_into`, mapping to `Error::AtMaxVestingSchedules`: [5](#0-4) [6](#0-5) [7](#0-6) 

This is structurally identical to the reported veALCX bug: a bounded, fixed-size, per-target list (`MAX_DELEGATES` there, `MAX_VESTING_SCHEDULES` here) that is appended to by third-party callers acting on their own resources but targeting someone else's account, with no cost or limit tied to *who* the attacker is relative to the victim, and no mechanism preventing many cheap/minimal calls from monopolizing the victim's slot capacity. Just as Bob used 1024 nearly-free `createLock`+`delegate` calls to fill Alex's `MAX_DELEGATES` slots, an attacker can use `MAX_VESTING_SCHEDULES` calls to `vested_transfer` (each only required to meet `MinVestedTransfer`, a chain-configured minimum that is independent of the victim and can be arbitrarily small relative to an attacker's total capital, and can be reused indefinitely as accounts vest out and schedules get pruned) to fill the victim's `Vesting` entry.

The only recovery mechanism available to the victim is `merge_schedules`, which the victim must call themselves to consolidate schedules and free slot capacity — precisely analogous to Alex having to actively "undelegate" to clear the griefing votes in the original report. The attacker can then repeat the griefing.

### Impact Explanation
This falls under "public underpriced work that degrades... chain... processing" and permanent/temporary "user-fund... lock" categories in spirit: a victim who is expecting a legitimate vested transfer (e.g., payroll, grant, team allocation, exchange payout) can be denied receiving it because their `Vesting` storage entry is already saturated with dust schedules from an attacker, and `force_vested_transfer` (root-only) or ordinary `vested_transfer` calls targeting them will revert with `AtMaxVestingSchedules`. This is a real availability/DoS impact on an unprivileged account with no admin, governance, validator, or off-chain trust assumptions required — the attacker is a plain unprivileged extrinsic sender.

### Likelihood Explanation
Likelihood is a function of `MinVestedTransfer` and `MAX_VESTING_SCHEDULES` values configured per runtime, which were not fully confirmed for every production chain within available search results; `MAX_VESTING_SCHEDULES` is a small fixed constant (bounded list) while `MinVestedTransfer` is runtime-configured and, in test/mock configurations, can be as small as `MinVestedTransfer` from `substrate/frame/vesting/src/tests.rs` demonstrates filling to `max_schedules` and then reverting with `AtMaxVestingSchedules`: [8](#0-7) 
This test directly confirms the exhaustible, per-target nature of the cap. Actual exploitability on a given production chain depends on the relation between `MinVestedTransfer` and the attacker's available capital across `MAX_VESTING_SCHEDULES` calls — I was not able to fully verify the exact configured constants in this session (search was interrupted at final iteration), so likelihood on live networks should be independently confirmed against `polkadot/runtime/westend/src/lib.rs`, `polkadot/runtime/rococo/src/lib.rs`, and `substrate/bin/node/runtime/src/lib.rs`.

### Recommendation
- Track the schedule "source"/depositor per entry (or track how many distinct callers/how much total dust each source has contributed to a target) and expose a per-source cap independent of `MAX_VESTING_SCHEDULES`, or
- Require that a `vested_transfer` targeting an account not owned by the sender pay a scaling deposit reserved from the sender per schedule (similar to elections-phragmen's per-vote deposit), refundable once the schedule fully vests, so griefing has a real, recoverable-only-by-attacker cost; and/or
- Allow the target (or an authorized origin) to reject/cap incoming third-party vested transfers, or auto-merge/evict the smallest/oldest schedule when at capacity rather than hard-failing new legitimate transfers.

### Proof of Concept
Conceptual reproduction using the existing pallet-vesting test harness pattern (`substrate/frame/vesting/src/tests.rs`):
```rust
// Attacker (any account, e.g. account 13) sends MAX_VESTING_SCHEDULES minimal
// vested transfers to the victim (account 4), each meeting only MinVestedTransfer.
let max_schedules = <Test as Config>::MAX_VESTING_SCHEDULES;
let sched = VestingInfo::new(<Test as Config>::MinVestedTransfer::get(), 1, 10);

for _ in 0..max_schedules {
    assert_ok!(Vesting::vested_transfer(Some(13).into(), 4, sched));
}

// Now a legitimate sender (e.g. account 3, employer/exchange) tries to pay
// the victim a real vested allocation and is blocked:
assert_noop!(
    Vesting::vested_transfer(Some(3).into(), 4, sched),
    Error::<Test>::AtMaxVestingSchedules,
);
```
This mirrors `vested_transfer_allows_max_schedules` in `substrate/frame/vesting/src/tests.rs` (lines 415-453) but reframed with the "filler" schedules coming from an attacker account distinct from the intended legitimate payer, demonstrating the griefing of account 4's ability to receive vested payouts from anyone else, cheaply and repeatably.

### Citations

**File:** substrate/frame/vesting/src/lib.rs (L226-233)
```rust
	/// Information regarding the vesting of a given account.
	#[pallet::storage]
	pub type Vesting<T: Config> = StorageMap<
		_,
		Blake2_128Concat,
		T::AccountId,
		BoundedVec<VestingInfo<BalanceOf<T>, BlockNumberFor<T>>, MaxVestingSchedulesGet<T>>,
	>;
```

**File:** substrate/frame/vesting/src/lib.rs (L298-312)
```rust
	/// Error for the vesting pallet.
	#[pallet::error]
	pub enum Error<T> {
		/// The account given is not vesting.
		NotVesting,
		/// The account already has `MaxVestingSchedules` count of schedules and thus
		/// cannot add another one. Consider merging existing schedules in order to add another.
		AtMaxVestingSchedules,
		/// Amount being transferred is too low to create a vesting schedule.
		AmountLow,
		/// An index was out of bounds of the vesting schedules.
		ScheduleIndexOutOfBounds,
		/// Failed to create a new schedule because some parameter was invalid.
		InvalidScheduleParams,
	}
```

**File:** substrate/frame/vesting/src/lib.rs (L368-380)
```rust
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

**File:** substrate/frame/vesting/src/lib.rs (L640-652)
```rust
		let schedules: BoundedVec<
			VestingInfo<BalanceOf<T>, BlockNumberFor<T>>,
			MaxVestingSchedulesGet<T>,
		> = schedules.try_into().map_err(|_| Error::<T>::AtMaxVestingSchedules)?;

		if schedules.len() == 0 {
			Vesting::<T>::remove(&who);
		} else {
			Vesting::<T>::insert(who, schedules)
		}

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

**File:** substrate/frame/vesting/src/tests.rs (L415-453)
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
		assert_eq!(Balances::free_balance(&4), user_4_free_balance);

		// Account 4 has fully vested when all the schedules end,
		System::set_block_number(
			<Test as Config>::MinVestedTransfer::get() + sched.starting_block(),
		);
		assert_eq!(Vesting::vesting_balance(&4), Some(0));
		// and after unlocking its schedules are removed from storage.
		vest_and_assert_no_vesting::<Test>(4);
	});
```
