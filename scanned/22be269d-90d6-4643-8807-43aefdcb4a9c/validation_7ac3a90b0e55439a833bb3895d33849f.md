### Title
Unconsented `pallet_vesting::vested_transfer` lets an attacker grief a victim's `MAX_VESTING_SCHEDULES` slots, DoS'ing legitimate vested transfers to that account - (File: `substrate/frame/vesting/src/lib.rs`)

### Summary
`pallet_vesting::vested_transfer` (and its `pallet-revive` precompile wrapper) lets **any signed account** push a vesting schedule onto a **target** account without that target's consent, mirroring the reported `BobStaking::stake()`/`BonusWrapper::stake()` pattern where an unrelated caller can mutate a victim's receiver-side state. Because each account has a hard cap of `T::MAX_VESTING_SCHEDULES` concurrent schedules, an attacker can repeatedly call `vested_transfer` with the minimum allowed amount (`T::MinVestedTransfer`) targeting the same victim until all schedule slots are consumed, after which any further legitimate `vested_transfer`/`force_vested_transfer` to that victim (e.g. from an exchange, employer, or the runtime's own vesting logic) fails with `AtMaxVestingSchedules`.

### Finding Description
`vested_transfer` is dispatchable by any signed origin and only validates the `schedule` itself, not who the receiver is or whether the receiver already has state that conflicts with attacker intent: [1](#0-0) 

The core state mutation happens in `do_vested_transfer`, which calls `can_add_vesting_schedule` -> `add_vesting_schedule`. The only guard against repeated calls is the account-wide bound `T::MAX_VESTING_SCHEDULES`, enforced via `BoundedVec::try_push`: [2](#0-1) [3](#0-2) 

There is no `receiver == sender` check and no per-sender rate limiting comparable to the reported bug's `lockPeriod` consistency check — here the analogous "guard" (`AtMaxVestingSchedules`) protects the pallet's storage bound, not the receiver's intent. An attacker who knows a victim's address (e.g., a payroll/exchange account expecting incoming vested payouts) can call `vested_transfer` `T::MAX_VESTING_SCHEDULES` times with amounts at or slightly above `T::MinVestedTransfer`, filling every schedule slot for that account. Any subsequent, legitimate `vested_transfer`/`force_vested_transfer` (including root-privileged `force_vested_transfer` used by chain operators for genuine payouts) to the victim then reverts with `Error::AtMaxVestingSchedules`: [4](#0-3) 

The victim's only mitigation is calling `merge_schedules` to compact slots — but this requires the victim to notice the griefing and actively act, and the attacker can re-fill the freed slot(s) in the same or next block since the call is public and permissionless, reproducing an ongoing DoS exactly analogous to the reported staking lock-period griefing.

### Impact Explanation
This is a public-entrypoint DoS: an unprivileged attacker can permanently (until victim intervenes) block a target address from receiving further vested transfers, which can be used to block legitimate payroll/vesting payouts, disrupt token distribution schedules, or grief exchanges/treasuries that rely on `vested_transfer`/`force_vested_transfer` reaching a specific account. It does not steal funds directly, but it satisfies the "public underpriced work that degrades intended chain behavior" and "permanent user-fund/state lock" categories in scope, because it forces state (`Vesting<T>` storage bound) to a griefed condition using only dust-level `MinVestedTransfer` amounts, at ordinary transaction weight cost.

### Likelihood Explanation
Medium-High: the attack requires no special privileges, only knowledge of the target address and enough balance to cover `MAX_VESTING_SCHEDULES × MinVestedTransfer` (a chain-configured, generally small minimum). Because `vested_transfer` is a routine, low-friction extrinsic and precompiled for EVM callers via `pallet-revive` (`substrate/frame/vesting/precompiles/src/lib.rs`), attacker cost and complexity are low.

### Recommendation
Adopt the same fixes suggested in the analog report:
- **Option 1:** Require `receiver == origin` for `vested_transfer`, i.e. disallow crediting a vesting schedule to a third party without an explicit opt-in/claim step (a "pending vesting transfer" the target must accept), separate from the existing root-only `force_vested_transfer`.
- **Option 2:** If third-party vesting transfers must remain permissionless, decouple attacker-controlled schedule creation from the shared `MAX_VESTING_SCHEDULES` bound — e.g., reserve a portion of slots for self-initiated schedules, or require a minimum transfer amount high enough to make griefing all slots economically deterrent, combined with automatic `merge_schedules`-style compaction when a new schedule is added and the account is at capacity.

### Proof of Concept
Given `T::MAX_VESTING_SCHEDULES = N` and `T::MinVestedTransfer = M` (test config values, see `substrate/frame/vesting/src/tests.rs`):
1. Victim `V` has zero vesting schedules.
2. Attacker `A` (any funded account) calls `Vesting::vested_transfer(Some(A).into(), V, VestingInfo::new(M, 1, start))` `N` times in a row — each succeeds and increments `Vesting::<T>::get(V).len()`, matching the pattern demonstrated by the existing test: [5](#0-4) 
3. A legitimate third party `P` (e.g., payroll) now calls `Vesting::vested_transfer(Some(P).into(), V, real_schedule)` and receives `Error::<T>::AtMaxVestingSchedules`, exactly mirroring the reverted `InconsistentLockPeriod` scenario in the external report's PoC.
4. `V` must notice this and call `merge_schedules` to recover a slot, but `A` can immediately repeat step 2 to re-fill it, sustaining the DoS.

Note: I was not able to fully confirm the exact numeric values of `MinVestedTransfer`/`MAX_VESTING_SCHEDULES` for each production runtime (e.g. `polkadot/runtime/westend/src/lib.rs`, `cumulus/parachains/runtimes/assets/asset-hub-westend/src/lib.rs`) within the available tool budget — these values determine the precise attacker cost and should be checked directly in those files to size the real-world severity for each affected chain.

### Citations

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
