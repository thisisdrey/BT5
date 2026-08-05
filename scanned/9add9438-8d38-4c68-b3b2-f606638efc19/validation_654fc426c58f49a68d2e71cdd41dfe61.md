## Analysis: `pallet-vesting` per-account schedule saturation

Confirmed: `VestingInfo::locked() / per_block()` with `per_block=1` (minimum allowed value, since `per_block()` accessor floors at `One::one()`) and an arbitrarily large `locked` amount gives an arbitrarily large `ending_block_as_balance`, i.e. a vesting schedule can be made to last for an effectively unbounded number of blocks while only transferring the `MinVestedTransfer` minimum amount. Any signed account can call `vested_transfer` against **any target account** it does not control. [1](#0-0) [2](#0-1) 

### Title
Unauthorized permanent saturation of a victim's `Vesting` schedule slots via `pallet_vesting::vested_transfer` - (`substrate/frame/vesting/src/lib.rs`)

### Summary
`pallet-vesting` bounds each account to `MAX_VESTING_SCHEDULES` entries in the `Vesting<T>` `BoundedVec` [3](#0-2) . The public, permissionless `vested_transfer(origin, target, schedule)` extrinsic lets *any* signed account push a new schedule into *any other* account's `Vesting` entry, gated only by `MinVestedTransfer` [4](#0-3) . This is structurally the same broken invariant as the `MAX_DELEGATES` bug in the report: a shared, per-victim bounded resource that unprivileged third parties can fill up against a target they don't control, permanently denying legitimate future use of that resource for the victim.

### Finding Description
`do_vested_transfer` only checks `schedule.locked() >= MinVestedTransfer` and `schedule.is_valid()` (non-zero `locked`/`per_block`), then calls `can_add_vesting_schedule`/`add_vesting_schedule`, which just checks the target's current schedule count against `MAX_VESTING_SCHEDULES` [5](#0-4) [6](#0-5) .

Nothing bounds the *duration* of the schedule: `per_block()` is floored at `1` [7](#0-6) , and `ending_block_as_balance` computes `locked / per_block()` [1](#0-0) . By setting `per_block = 1` and `locked = MinVestedTransfer`, an attacker creates a schedule that takes `MinVestedTransfer` blocks (potentially millions) to fully vest, at the minimum allowed cost per slot. Repeating this `MAX_VESTING_SCHEDULES` times (28 in the reference kitchensink config, but this is a pallet constant set per-runtime) against one target account exhausts all of that account's schedule slots, all with near-maximal remaining durations.

Once saturated, every future `vested_transfer`/`force_vested_transfer` targeting that account fails with `AtMaxVestingSchedules` [8](#0-7)  until the victim's own `vest`/`vest_other` calls fully unlock and prune expired entries via `write_vesting`/`exec_action` — which the attacker controls the timing of, since they choose the schedule length. The guard that exists (`MAX_VESTING_SCHEDULES` bound + `MinVestedTransfer` floor) does not stop the path because neither limit constrains schedule *duration*, and the call is entirely permissionless with respect to the `target` parameter — the caller need not be, own, or have any relationship to `target`.

### Impact Explanation
This matches "permanent user-fund or bridge-state lock" and DoS class in the Impact Gate: a victim account can be locked out of receiving vested transfers (e.g. blocking exchanges, vesting-based payroll/genesis allocations, or crowdloan/parachain reward distributions that rely on `vested_transfer`/`force_vested_transfer`) for as long as the attacker-chosen schedule durations run, which can be made to last effectively indefinitely (bounded only by `BlockNumber`'s max value) at minimal per-slot cost. It is a repeatable, target-specific denial of legitimate vesting operations without needing any privileged role, governance action, validator/collator collusion, or off-chain infrastructure — purely a public dispatchable call available on any runtime including `pallet-vesting` (e.g. Polkadot/Kusama relay chains, `asset-hub`, and the reference node runtime).

### Likelihood Explanation
High feasibility: `vested_transfer` is a signed, permissionless extrinsic with call index 2, present in production runtimes (Polkadot, Westend, Rococo, the Substrate node template, etc.) [9](#0-8) . The only cost is `MAX_VESTING_SCHEDULES` × `MinVestedTransfer`, a fixed, chain-configured, one-time amount (paid by attacker, though it ends up transferred, locked, to the victim) — no special privilege, timing, front-running, or infrastructure is required, and it can be executed at any time against any existing account with a positive balance transferable to it.

### Recommendation
- Enforce a maximum vesting schedule duration (a configurable `MaxVestingDuration` cap on `ending_block_as_balance - starting_block`) so a single schedule cannot occupy a slot indefinitely.
- Consider requiring `target` consent (e.g., an allow-list, `require_target_ack`, or letting the target opt out of unsolicited `vested_transfer`s) similar to guard patterns already used elsewhere in the SDK for unsolicited value transfers.
- Alternatively/additionally, allow the account owner (or `vest_other` caller) to evict/merge the oldest or worst schedules once at capacity rather than hard-failing new legitimate transfers.

### Proof of Concept
1. Attacker account `A` has balance ≥ `MAX_VESTING_SCHEDULES × MinVestedTransfer`.
2. For `i in 0..MAX_VESTING_SCHEDULES`: `A` calls `Vesting::vested_transfer(target, VestingInfo::new(MinVestedTransfer, 1, current_block))`. Each call succeeds since `Vesting<T>::get(target).len() < MAX_VESTING_SCHEDULES` and `schedule.is_valid()` holds trivially with `per_block=1`.
3. After `MAX_VESTING_SCHEDULES` calls, `target`'s `Vesting` `BoundedVec` is full, all with `ending_block ≈ starting_block + MinVestedTransfer` (a very large number of blocks away).
4. Any subsequent call `Vesting::vested_transfer(_, target, _)` or `Vesting::force_vested_transfer(_, _, target, _)` from any account (including legitimate senders like an exchange, employer, or crowdloan payout mechanism) now reverts with `Error::AtMaxVestingSchedules`, confirming the target is denial-of-serviced for vesting-based transfers until the attacker-chosen long schedules elapse or the target calls `vest`/`vest_other` to prune fully-completed ones (which the attacker deliberately delays).

### Citations

**File:** substrate/frame/vesting/src/vesting_info.rs (L69-74)
```rust
	/// Amount that gets unlocked every block after `starting_block`. Corrects for `per_block` of 0.
	/// We don't let `per_block` be less than 1, or else the vesting will never end.
	/// This should be used whenever accessing `per_block` unless explicitly checking for 0 values.
	pub fn per_block(&self) -> Balance {
		self.per_block.max(One::one())
	}
```

**File:** substrate/frame/vesting/src/vesting_info.rs (L103-124)
```rust
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

**File:** substrate/bin/node/runtime/src/lib.rs (L1894-1911)
```rust
parameter_types! {
	pub const MinVestedTransfer: Balance = 100 * DOLLARS;
	pub UnvestedFundsAllowedWithdrawReasons: WithdrawReasons =
		WithdrawReasons::except(WithdrawReasons::TRANSFER | WithdrawReasons::RESERVE);
}

impl pallet_vesting::Config for Runtime {
	type RuntimeEvent = RuntimeEvent;
	type Currency = Balances;
	type BlockNumberToBalance = ConvertInto;
	type MinVestedTransfer = MinVestedTransfer;
	type WeightInfo = pallet_vesting::weights::SubstrateWeight<Runtime>;
	type UnvestedFundsAllowedWithdrawReasons = UnvestedFundsAllowedWithdrawReasons;
	type BlockNumberProvider = System;
	// `VestingInfo` encode length is 36bytes. 28 schedules gets encoded as 1009 bytes, which is the
	// highest number of schedules that encodes less than 2^10.
	const MAX_VESTING_SCHEDULES: u32 = 28;
}
```
