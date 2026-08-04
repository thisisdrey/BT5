### Title
Permissionless `vested_transfer` lets any account grief a victim by repeatedly filling their vesting schedule slots, permanently freezing part of their balance with attacker-controlled, ever-renewable lock durations - ([File: substrate/frame/vesting/src/lib.rs])

### Summary
`pallet-vesting::vested_transfer` (and its underlying `do_vested_transfer`) can be called by **any signed account** against **any target account**, with no consent or permission check from the target. Each call adds a new `VestingInfo` schedule to the target and (re)applies a `LockableCurrency` lock (`VESTING_ID`) on the target's balance via `write_lock`, sized to the maximum currently-locked amount across all of the target's schedules. Because schedule creation is fully attacker-controlled (amount just above `MinVestedTransfer`, and an arbitrarily long duration), and the number of concurrently held schedules is bounded only by `T::MAX_VESTING_SCHEDULES`, an attacker can occupy all of a victim's schedule slots with tiny, extremely long-duration schedules. This is directly analogous to the Sherlock finding: an unprivileged, low-cost action by a third party creates/renews a lock on someone else's account that they never asked for and cannot unilaterally clear, causing a lasting griefing/DoS condition on that account's usable balance and on the account's ability to ever receive further vesting benefit from anyone else (`AtMaxVestingSchedules`). [1](#0-0) [2](#0-1) [3](#0-2) 

### Finding Description
`vested_transfer` is a signed extrinsic with **no origin check tying the caller to the target** — `transactor` and `target` can be any two distinct accounts: [4](#0-3) 

`do_vested_transfer` only validates the schedule shape (`is_valid()`, `locked() >= MinVestedTransfer`) and that the target has fewer than `MAX_VESTING_SCHEDULES` existing schedules (`can_add_vesting_schedule`). It then transfers the funds and calls `add_vesting_schedule`, which pushes the new schedule and calls `write_lock`, applying/updating the `VESTING_ID` `LockableCurrency` lock on the **target's** account for the newly computed `locked_now` amount: [5](#0-4) 

`write_lock`/`update_locks` in `pallet-balances` computes `frozen` as the max of all locks (not additive per-lock), and this frozen amount reduces the account's usable/transferable balance regardless of who created the lock: [6](#0-5) 

The lock can only be reduced by calling `vest`/`vest_other` (permissionless unlock, anyone can trigger, but it merely realizes already-elapsed vesting) or `merge_schedules` (signed only by the account owner). Critically, the schedule *slot budget* is fixed at `MAX_VESTING_SCHEDULES` per account, and it is consumed by whichever account calls `vested_transfer` first — the target has no way to reject an unsolicited schedule, no way to prevent third parties from occupying its remaining slots, and no way to force-merge/clear third-party schedules without waiting out arbitrarily attacker-chosen long durations. This mirrors the root cause in the reported bug: a state-mutating action, gated only by the caller being *some* signed account (not the beneficiary), unconditionally imposes/renews a lock-like restriction on a third party's account, and the beneficiary has no way to opt out or immediately clear it.

### Impact Explanation
An attacker can:
1. Repeatedly call `vested_transfer(target, schedule)` with `locked = MinVestedTransfer` and a very large duration (small `per_block`), consuming one of the victim's limited `Vesting` schedule slots per call, until `Vesting::<T>::decode_len(target) == MAX_VESTING_SCHEDULES`.
2. This permanently (until the extremely long schedules unwind) occupies all the target's vesting slots, preventing the target from ever receiving a legitimate `vested_transfer`/`force_vested_transfer` (e.g., salary/reward vesting from an employer, treasury, or crowdloan payout mechanism) — any such attempt fails with `AtMaxVestingSchedules`.
3. Because `frozen` balance in `pallet-balances` is computed as the max across the account's locks (not the sum), the attacker's schedules also keep an active `VESTING_ID` lock on the target account that only decays as slowly as the attacker chose, restricting the target's spendable balance for the duration the attacker selected, without the target's consent.

This is a public, underpriced (attacker only risks `MinVestedTransfer`, which is returned to them over time once vested, or transferred at negligible economic cost relative to the disruption) means for an unprivileged account to impose a lasting fund-lock / denial-of-benefit condition on any other account on chain — matching the "permanent user-fund … lock" and "public underpriced work" impact classes in scope.

### Likelihood Explanation
The attack requires only:
- A signed account with balance ≥ `MinVestedTransfer` (a pallet-configurable, typically small, minimum).
- Knowledge of the victim's `AccountId` (public).
- No privileged origin, no governance, no malicious validator/collator/relayer — a plain unprivileged extrinsic caller.

This is straightforward to execute repeatedly (`MAX_VESTING_SCHEDULES` calls), each a normal signed transaction, and does not depend on timing races, front-running, or any other party's misbehavior.

### Recommendation
Restrict `vested_transfer`/`force_vested_transfer` so that unsolicited third-party schedules cannot exhaust a victim's schedule slots or impose locks without consent, e.g.:
- Require target opt-in (e.g., a `permit`-style pre-approval or a `min_amount`/allow-list the target sets) before an arbitrary third party can create a vesting schedule for them, or
- Reserve/segregate schedule slots so a fixed number are always available for schedules the target itself initiated or approved, or
- Allow the target (or a privileged origin) to force-merge/cancel schedules created by third parties, rather than only via `merge_schedules` (self-only) or `force_remove_vesting_schedule` (root-only).

### Proof of Concept
Given `MAX_VESTING_SCHEDULES = N` and `MinVestedTransfer = m`:
1. Attacker `A` with balance `≥ N*m` calls, for `i in 1..=N`:
   `Vesting::vested_transfer(A, target, VestingInfo::new(m, 1, very_large_starting_block_or_tiny_per_block))`
   Each call succeeds per `do_vested_transfer` -> `can_add_vesting_schedule` (only checks count `< MAX_VESTING_SCHEDULES`), transfers `m` from `A` to `target`, and calls `add_vesting_schedule`, which pushes the schedule and calls `write_lock` on `target`. [2](#0-1) 
2. After `N` calls, `target`'s `Vesting` storage is full; any subsequent legitimate `vested_transfer`/`force_vested_transfer` to `target` (e.g., from an employer or the treasury) fails with `Error::AtMaxVestingSchedules`. [3](#0-2) 
3. `target`'s spendable balance remains reduced by `frozen = max(locks)` (per `pallet-balances::update_locks`) for as long as the attacker's chosen (long) durations keep `locked_at(now) > 0`, and `target` cannot unilaterally remove third-party schedules (`merge_schedules` requires being called by `target` itself but does not let them evict attacker schedules faster than the schedule's own decay; there is no "reject incoming schedule" call). [6](#0-5)

### Citations

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

**File:** substrate/frame/vesting/src/lib.rs (L768-816)
```rust
	/// Adds a vesting schedule to a given account.
	///
	/// If the account has `MaxVestingSchedules`, an Error is returned and nothing
	/// is updated.
	///
	/// On success, a linearly reducing amount of funds will be locked. In order to realise any
	/// reduction of the lock over time as it diminishes, the account owner must use `vest` or
	/// `vest_other`.
	///
	/// It is a no-op if the amount to be vested is zero.
	///
	/// NOTE: This doesn't alter the free balance of the account.
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

**File:** substrate/frame/vesting/src/lib.rs (L818-837)
```rust
	/// Ensure we can call `add_vesting_schedule` without error. This should always
	/// be called prior to `add_vesting_schedule`.
	fn can_add_vesting_schedule(
		who: &T::AccountId,
		locked: BalanceOf<T>,
		per_block: BalanceOf<T>,
		starting_block: BlockNumberFor<T>,
	) -> DispatchResult {
		// Check for `per_block` or `locked` of 0.
		if !VestingInfo::new(locked, per_block, starting_block).is_valid() {
			return Err(Error::<T>::InvalidScheduleParams.into());
		}

		ensure!(
			(Vesting::<T>::decode_len(who).unwrap_or_default() as u32) < T::MAX_VESTING_SCHEDULES,
			Error::<T>::AtMaxVestingSchedules
		);

		Ok(())
	}
```

**File:** substrate/frame/balances/src/lib.rs (L1158-1214)
```rust
		pub(crate) fn update_locks(who: &T::AccountId, locks: &[BalanceLock<T::Balance>]) {
			let bounded_locks = WeakBoundedVec::<_, T::MaxLocks>::force_from(
				locks.to_vec(),
				Some("Balances Update Locks"),
			);

			if locks.len() as u32 > T::MaxLocks::get() {
				log::warn!(
					target: LOG_TARGET,
					"Warning: A user has more currency locks than expected. \
					A runtime configuration adjustment may be needed."
				);
			}
			let freezes = Freezes::<T, I>::get(who);
			let mut prev_frozen = Zero::zero();
			let mut after_frozen = Zero::zero();
			// We do not alter ED, so the account will not get dusted. Yet, consumer limit might be
			// full, therefore we pass `true` into `mutate_account` to make sure this cannot fail
			let res = Self::mutate_account(who, true, |b| {
				prev_frozen = b.frozen;
				b.frozen = Zero::zero();
				for l in locks.iter() {
					b.frozen = b.frozen.max(l.amount);
				}
				for l in freezes.iter() {
					b.frozen = b.frozen.max(l.amount);
				}
				after_frozen = b.frozen;
			});
			match res {
				Ok((_, None)) => {
					// expected -- all good.
				},
				Ok((_, Some(_dust))) => {
					Self::deposit_event(Event::Unexpected(UnexpectedKind::BalanceUpdated));
					defensive!("caused unexpected dusting/balance update.");
				},
				_ => {
					Self::deposit_event(Event::Unexpected(UnexpectedKind::FailedToMutateAccount));
					defensive!("errored in mutate_account");
				},
			}

			match locks.is_empty() {
				true => Locks::<T, I>::remove(who),
				false => Locks::<T, I>::insert(who, bounded_locks),
			}

			if prev_frozen > after_frozen {
				let amount = prev_frozen.saturating_sub(after_frozen);
				Self::deposit_event(Event::Unlocked { who: who.clone(), amount });
			} else if after_frozen > prev_frozen {
				let amount = after_frozen.saturating_sub(prev_frozen);
				Self::deposit_event(Event::Locked { who: who.clone(), amount });
			}
		}

```
