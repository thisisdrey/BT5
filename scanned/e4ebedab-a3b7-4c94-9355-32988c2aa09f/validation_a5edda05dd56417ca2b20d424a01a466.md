### Title
Permanent DoS of `pallet-vesting` via permissionless `vested_transfer` schedule-slot exhaustion - (File: `substrate/frame/vesting/src/lib.rs`)

### Summary
`Pallet::vested_transfer` lets any signed account push a new vesting schedule into an **arbitrary target account's** `Vesting<T>` list, bounded only by `T::MAX_VESTING_SCHEDULES`. An attacker can permissionlessly fill a victim's schedule slots with long-duration, dust-value schedules, permanently blocking that account from receiving any further legitimate vesting transfers (employer payouts, crowdloan/parachain reward vesting, etc.), until the victim manually intervenes.

### Finding Description
`vested_transfer` is dispatched by `transactor` and lets it choose any `target`: [1](#0-0) 

It calls `do_vested_transfer`, which validates the schedule and calls `add_vesting_schedule` for `target`: [2](#0-1) 

`add_vesting_schedule` pushes into the target's `BoundedVec` and only fails once the bound is hit: [3](#0-2) 

The only guard is `schedule.locked() >= T::MinVestedTransfer::get()` and `schedule.is_valid()` (non-zero `per_block`), enforced in `do_vested_transfer`: [4](#0-3) 

Nothing prevents the attacker from choosing `locked = MinVestedTransfer` and `per_block = 1` (or another minimal value), which — for any sizeable `locked` denomination — makes the schedule take an enormous number of blocks to fully vest. This occupies one of the victim's limited schedule slots (`T::MAX_VESTING_SCHEDULES`, e.g. 28 in many runtime configs) for a duration that is effectively permanent from the perspective of block production. Repeating the attack `MAX_VESTING_SCHEDULES` times fully exhausts the victim's slots. This is the direct analog of the external report's "push into another user's unbounded/limited list via a public entry point" primitive: `deposit()` pushing into `nftIdsStaked[victim]` ↔ `vested_transfer()` pushing into `Vesting::<T>::get(target)`.

Once the bound is reached, any subsequent legitimate `vested_transfer`, `force_vested_transfer`, or `VestedTransfer`/`VestingSchedule` trait calls targeting the same account fail with `Error::AtMaxVestingSchedules`, since `try_push` simply errors out with no eviction or prioritization logic.

### Impact Explanation
This matches the "permanent user-fund lock" / DoS class in scope: a victim account can be permanently prevented from receiving new vesting-based value transfers (staking/crowdloan reward vesting, salary vesting, treasury vesting payouts) by an unprivileged attacker who spends only `MinVestedTransfer` amount per slot — and that value is not even lost, it is transferred to the victim (just parked in a near-unusable long schedule). The severity is lower than "loss of funds" because the attacker's outlay ends up credited to the victim, but it is a genuine, low-cost, permissionless denial-of-service against a specific account's ability to receive vesting.

### Likelihood Explanation
Likelihood is limited by two mitigating factors mirroring the external report's own severity downgrade:
- The victim can call `merge_schedules` (self-service `Call` on `pallet-vesting`) to combine two of their own schedules — including attacker-planted ones — into a single slot, freeing capacity, similar to how the Biconomy victim could still withdraw NFTs one at a time.
- The attack costs the attacker `MinVestedTransfer * (number of slots to fill)` in transferred value (which the victim receives, diminishing attacker incentive versus a pure griefing/DoS with zero cost).

Because of these mitigations and the requirement that the attacker permanently commit real value to the victim, this is a real but Medium/Low-severity griefing vector rather than a fund-theft or chain-halting bug.

### Recommendation
- Consider adding a per-sender rate limit or an explicit "reject inbound vesting schedule" preference for target accounts (opt-in vesting transfers), or
- Auto-merge/replace the smallest/oldest schedule when a `try_push` would fail and the new schedule is materially larger in value, or
- Expose a permissionless "compact my own schedules" extrinsic prominently and consider increasing/parameterizing `MAX_VESTING_SCHEDULES` for high-traffic accounts, or
- Require the caller to be the target, or require target opt-in/allow-list, for third-party `vested_transfer` (analogous to `ClaimPermission` in `pallet-nomination-pools`, which restricts `bond_extra_other`/`claim_payout_other` unless the target explicitly grants permission).

### Proof of Concept
1. Attacker holds `MinVestedTransfer` funds.
2. For `i in 0..MAX_VESTING_SCHEDULES`, attacker calls:
   `Vesting::vested_transfer(Signed(attacker), target_lookup, VestingInfo::new(MinVestedTransfer, 1, current_block))`
   Each call succeeds (`do_vested_transfer` → `add_vesting_schedule` → `try_push`) since `locked >= MinVestedTransfer` and `per_block != 0` satisfy validity checks: [4](#0-3) 
3. After `MAX_VESTING_SCHEDULES` calls, `Vesting::<T>::get(target).len() == T::MAX_VESTING_SCHEDULES`.
4. Any further `vested_transfer`/`force_vested_transfer` to `target` now fails with `Error::AtMaxVestingSchedules` from `add_vesting_schedule`: [5](#0-4) 
5. Because `per_block = 1` while `locked` is denominated in the chain's smallest unit multiplied by `MinVestedTransfer` (often several existential deposits), full vesting of each planted schedule can take on the order of `locked` blocks — effectively occupying the slot indefinitely relative to normal operation, unless the victim proactively calls `merge_schedules` to consolidate schedules.

Note: I was unable to fully trace `merge_schedules`'s call-site signature and `MAX_VESTING_SCHEDULES` concrete values across all shipped runtimes within the available tool budget; if further precision on default bound sizes (e.g., asset-hub, relay chains) is needed, a full repository session should be used to confirm exact runtime `MaxVestingSchedules`/`MinVestedTransfer` configuration values.

### Citations

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

**File:** substrate/frame/vesting/src/lib.rs (L780-801)
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

```
