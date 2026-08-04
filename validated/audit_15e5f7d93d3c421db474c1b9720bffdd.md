### Title
Unauthenticated `vested_transfer` lets anyone fill a victim's vesting-schedule slots with a tiny grant and permanently DoS legitimate vesting operations - (File: `substrate/frame/vesting/src/lib.rs`)

### Summary
`pallet_vesting::vested_transfer` (call index 2) is a fully public, signed-only extrinsic that lets *any* account create a new vesting schedule for *any other* `target` account, using the caller's own tokens, with no permission from or benefit check for the target. [1](#0-0) 

### Finding Description
`vested_transfer` only checks that the caller is signed and that the schedule meets `MinVestedTransfer`/validity constraints; it never checks any relationship between caller and `target`: [2](#0-1) 

The actual work is done in `do_vested_transfer`, which transfers `schedule.locked()` from `source` to `target` and then calls `add_vesting_schedule` for `target`: [3](#0-2) 

Each account has a **bounded** number of concurrent vesting schedules, `T::MAX_VESTING_SCHEDULES`, enforced by `BoundedVec<... , MaxVestingSchedulesGet<T>>` storage and checked in `can_add_vesting_schedule` before a new schedule is added. The vesting docs and the Solidity mirror confirm this: `vestedTransfer` "Reverts if ... `target` already has the maximum number of vesting schedules." [4](#0-3) 

This is the direct structural analog of the reported `vestFor` bug: an unauthenticated public function that mutates another account's locked-balance state (here, consuming a scarce per-account resource — one of the limited vesting-schedule slots) with only the attacker's own tiny `MinVestedTransfer`-sized stake, and no way for the victim to prevent or unwind it (schedules can only be removed by governance via `force_remove_vesting_schedule`, which requires `ensure_root`, or by waiting out the attacker-chosen (potentially very long) vesting duration). [5](#0-4) 

An attacker can call `vested_transfer(origin, target, schedule)` `T::MAX_VESTING_SCHEDULES` times (paying only `MinVestedTransfer` each time, e.g. 1 token per call, and setting `starting_block` far in the future / `per_block` tiny to make each schedule last as long as possible) to occupy every schedule slot belonging to `target`. Once all slots are consumed, any subsequent legitimate `vested_transfer`/`force_vested_transfer` aimed at `target` (e.g. from an employer, exchange, or vesting program) fails with the "maximum number of vesting schedules" error, permanently blocking the intended vesting/lock-up until the attacker's junk schedules fully vest (which the attacker controls, e.g. up to whatever the runtime allows for `starting_block`/`ending_block`) or root intervenes.

### Impact Explanation
This is a public-entrypoint griefing/DoS vector against arbitrary accounts: with a minimal, attacker-controlled cost (`MinVestedTransfer` per call), an unprivileged actor can lock a victim out of receiving further vested transfers for a duration entirely of the attacker's choosing, mirroring the original `vestFor` finding's "lock the user for 365 days with a tiny amount, no way to update" pattern. It matches the "permanent user-fund or bridge-state lock" and "public underpriced work" impact classes called out in the assessment scope, since a single small transaction from any account permanently consumes state slots belonging to another account's balance-lock subsystem.

### Likelihood Explanation
Likelihood is high: the call requires no special origin (`ensure_signed` only), no interaction with or consent from the target, and the cost is bounded by `MinVestedTransfer` (a small, protocol-defined minimum) times `MAX_VESTING_SCHEDULES` — an inexpensive, fully deterministic, repeatable attack requiring no privileged role, node, validator, or front-running condition.

### Recommendation
Require the `target` to opt in (e.g., via a pre-registered allowance/whitelist of senders, similar to the `Converter` whitelist recommended in the original report), or bound how many "unsolicited" schedules an arbitrary sender may create per target, and/or let the target proactively reject/cancel unsolicited vesting schedules without requiring root (`force_remove_vesting_schedule` currently requires `ensure_root`). Alternatively, decouple "received schedule slots created by unrelated third parties" from the slots reserved for the account's own/known-counterparty vesting, so third-party `vested_transfer` calls cannot exhaust the same bounded slot count used by legitimate vesting flows.

### Proof of Concept
1. Attacker account `A` has balance ≥ `MinVestedTransfer * MAX_VESTING_SCHEDULES`.
2. For `i in 0..MAX_VESTING_SCHEDULES`: `A` calls `Vesting::vested_transfer(origin=A, target=Victim, schedule=VestingInfo::new(MinVestedTransfer, 1, VERY_LARGE_STARTING_BLOCK))` — each call succeeds because `can_add_vesting_schedule` only checks slot count and validity, not any relation to `Victim`. [6](#0-5) 
3. After `MAX_VESTING_SCHEDULES` calls, `Victim`'s `Vesting` storage `BoundedVec` is full.
4. Any legitimate actor (employer, exchange, `force_vested_transfer` from governance targeting `Victim`) now fails with `Error::<T>::AtMaxVestingSchedules`-style rejection inside `can_add_vesting_schedule`, matching the documented revert condition in the precompile interface. [7](#0-6) 
5. `Victim` remains blocked from receiving further vesting grants until the attacker's junk schedules fully vest (attacker-chosen duration) or root calls `force_remove_vesting_schedule` on their behalf.

Note: I was not able to fully verify the exact numeric value of `MAX_VESTING_SCHEDULES` or `MinVestedTransfer` configured for the shipped runtimes (only test/mock config references were found within the indexed content), so the precise cost/duration bound of this DoS in production runtimes should be confirmed by inspecting the relevant runtime config (`substrate/frame/vesting/src/mock.rs` and each production runtime's `impl pallet_vesting::Config`).

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

**File:** substrate/frame/vesting/src/lib.rs (L462-490)
```rust
		/// Force remove a vesting schedule
		///
		/// The dispatch origin for this call must be _Root_.
		///
		/// - `target`: An account that has a vesting schedule
		/// - `schedule_index`: The vesting schedule index that should be removed
		#[pallet::call_index(5)]
		#[pallet::weight(
			T::WeightInfo::force_remove_vesting_schedule(MaxLocksOf::<T>::get(), T::MAX_VESTING_SCHEDULES)
		)]
		pub fn force_remove_vesting_schedule(
			origin: OriginFor<T>,
			target: <T::Lookup as StaticLookup>::Source,
			schedule_index: u32,
		) -> DispatchResultWithPostInfo {
			ensure_root(origin)?;
			let who = T::Lookup::lookup(target)?;

			let schedules_count = Vesting::<T>::decode_len(&who).unwrap_or_default();
			ensure!(schedule_index < schedules_count as u32, Error::<T>::InvalidScheduleParams);

			Self::remove_vesting_schedule(&who, schedule_index)?;

			Ok(Some(T::WeightInfo::force_remove_vesting_schedule(
				MaxLocksOf::<T>::get(),
				schedules_count as u32,
			))
			.into())
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

**File:** substrate/frame/vesting/precompiles/IVesting.sol (L42-50)
```text
	/// Transfer funds from the caller to `target` with an attached vesting schedule.
	///
	/// The caller must have sufficient free balance to cover `locked`.
	/// A new vesting schedule is created for `target` that linearly unlocks
	/// `perBlock` tokens per block starting at `startingBlock`.
	///
	/// Reverts if `locked` is below the runtime's `MinVestedTransfer`, if
	/// `perBlock` is zero, or if `target` already has the maximum number of
	/// vesting schedules.
```
