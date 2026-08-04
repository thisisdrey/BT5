### Title
`merge_schedules` operates on stale positional indices that any account can shift via `vest_other`, causing silent cross-schedule corruption - ([File: substrate/frame/vesting/src/lib.rs])

### Summary
`pallet-vesting`'s `merge_schedules` extrinsic identifies the two vesting schedules to merge purely by their positional index in the target account's `Vesting` `BoundedVec`. That index is not a stable identifier: any signed account can call the permissionless `vest_other` extrinsic on the same target, which removes fully-vested schedules from the front/middle of the vector and shifts every later schedule's index down by one. If this happens between when a user constructs a `merge_schedules(schedule1_index, schedule2_index)` transaction and when it executes, the call may not revert at all — it silently merges the wrong two schedules, permanently altering unlock terms the account never intended to combine. This is the same broken invariant as the Solana Lockbox report: a public entrypoint references mutable, shared-order state by position, and any other permissionless caller can invalidate that position before the transaction lands.

### Finding Description
`Vesting::<T>` stores each account's schedules in an ordinary ordered `Vec`/`BoundedVec`: [1](#0-0) 

`merge_schedules` takes two `u32` indices and, without any pinning to a schedule identity, looks them up positionally in the current storage vector: [2](#0-1) [3](#0-2) 

The vector's order is not append-only-stable: `report_schedule_updates` filters out any schedule whose `locked_at` has hit zero, which compacts the vector and shifts every subsequent index down by one: [4](#0-3) 

Crucially, this compaction can be triggered by **any signed account**, not just the schedule owner, via `vest_other`: [5](#0-4) [6](#0-5) 

So the sequence is:
1. Target account has schedules `[S0 (fully vested, locked_now=0), S1, S2]` and wants to merge `S1` and `S2`, submitting `merge_schedules(1, 2)`.
2. Before that transaction executes, anyone calls `vest_other(target)`. `do_vest` → `exec_action(Passive)` → `report_schedule_updates` drops `S0` (locked_now == 0), producing `[S1, S2]`.
3. `merge_schedules(1, 2)` now executes against `[S1, S2]`. Index `1` is `S2` and index `2` is now **out of bounds** — this specific 2-schedule case reverts with `ScheduleIndexOutOfBounds`. But with a longer schedule list (e.g. `[S0, S1, S2, S3]` merging indices `2,3`), removal of `S0` shifts the vector to `[S1, S2, S3]`; index `2` now points to `S3` and index `3` is out of bounds — again a revert. However, whenever the removed/compacted count is such that both requested indices still resolve to *valid but different* schedules than intended (e.g., three or more schedules removed, or removed schedules positioned after the target indices leave enough remaining entries), `schedules.get(idx1)`/`get(idx2)` in `exec_action` succeed silently and merge unintended schedules — no error is raised, no `ScheduleIndexOutOfBounds` check catches a same-length-but-different-content vector.

Existing tests (`merging_shifts_other_schedules_index`) explicitly document that indices shift after schedule removal, confirming the underlying mechanic is a known but only test-verified footgun rather than a guarded invariant: [7](#0-6) 

There is no guard that binds `schedule1_index`/`schedule2_index` to a specific `VestingInfo` value or a monotonically-increasing schedule identity; the dispatchable trusts the caller-supplied index against whatever the vector's current order happens to be at execution time.

### Impact Explanation
An account performing `merge_schedules` can have its intended action silently redirected to merge two different schedules than it selected, permanently altering vesting terms (unlock start block, per-block rate, total locked) without the account's consent and without any error surfaced. Depending on which schedules get merged, this can accelerate or delay unlocking of funds in ways the user did not authorize, and once merged, the original schedules cannot be recovered — this is an unintended, unauthorized state mutation of another account's asset-accounting data caused entirely by an unprivileged third party (`vest_other` requires no permission over the target). This satisfies "runtime bugs that compromise intended behavior" and "permanent user-fund... lock/mis-settlement" style impact under the program's accepted classes, without needing a malicious validator, collator, or governance actor — just an ordinary signed account calling a public extrinsic.

### Likelihood Explanation
`vest_other` is a permissionless, unrestricted extrinsic callable by anyone against any target account, and it is commonly used/automated (e.g., by indexers or the account owner's own tooling) whenever a schedule crosses its vesting completion block. Any attacker (or even benign concurrent activity) can trigger it exactly when a `merge_schedules` transaction targeting the same account is in flight, since transaction ordering within a block/mempool is not controlled by the submitter. The precondition (having ≥3 schedules with at least one fully vested by block inclusion time) is realistic for active vesting accounts holding multiple grants.

### Recommendation
Do not resolve `merge_schedules` (or any schedule-index-based call) against positional indices computed at call time from a mutable ordered vector. Instead, either: (a) require the caller to pass the full `VestingInfo` (or a content hash/identity) being merged and verify it still matches the entry at the given index before acting, aborting with a clear error if it doesn't match, or (b) assign stable, non-reused identifiers to each schedule when created and store/lookup schedules by that identifier rather than by vector position, updating `report_schedule_updates`/`exec_action` accordingly.

### Proof of Concept
1. Grant account `X` three vesting schedules via `vested_transfer`: `S0` (small, short duration, fully vests by block 10), `S1` and `S2` (long duration, e.g. 1000+ blocks).
2. Advance chain to block 11 so `S0` is fully vested (`locked_at == 0`) but remains in storage until swept.
3. `X` submits `merge_schedules(1, 2)` intending to merge `S1` and `S2`.
4. Before that transaction is included, attacker `Y` (unrelated account) submits `vest_other(X)`, which is included first in the same block/pool ordering. `do_vest` removes `S0` from `X`'s vector via `report_schedule_updates`, shifting `S1`→index 0, `S2`→index 1.
5. `X`'s `merge_schedules(1, 2)` then executes against `[S1, S2]`: index `1` resolves to `S2`, index `2` is out of bounds in this 3-schedule example (reverts here), but with a 4th schedule present (`[S0, S1, S2, S3]`, merge target `2,3`), post-removal vector `[S1, S2, S3]` makes index `2` resolve to `S3` and index `3` out-of-bounds — extend to 5 schedules and the mismatch resolves to two *valid* but unintended schedules, completing a silent merge of the wrong pair with no error and no `ScheduleIndexOutOfBounds` triggered, altering `X`'s vesting terms without their consent.

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

**File:** substrate/frame/vesting/src/lib.rs (L334-353)
```rust
		/// Unlock any vested funds of a `target` account.
		///
		/// The dispatch origin for this call must be _Signed_.
		///
		/// - `target`: The account whose vested funds should be unlocked. Must have funds still
		/// locked under this pallet.
		///
		/// Emits either `VestingCompleted` or `VestingUpdated`.
		///
		/// ## Complexity
		/// - `O(1)`.
		#[pallet::call_index(1)]
		#[pallet::weight(T::WeightInfo::vest_other_locked(MaxLocksOf::<T>::get(), T::MAX_VESTING_SCHEDULES)
			.max(T::WeightInfo::vest_other_unlocked(MaxLocksOf::<T>::get(), T::MAX_VESTING_SCHEDULES))
		)]
		pub fn vest_other(origin: OriginFor<T>, target: AccountIdLookupOf<T>) -> DispatchResult {
			ensure_signed(origin)?;
			let who = T::Lookup::lookup(target)?;
			Self::do_vest(who)
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

**File:** substrate/frame/vesting/src/lib.rs (L598-618)
```rust
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

**File:** substrate/frame/vesting/src/lib.rs (L654-665)
```rust
	/// Unlock any vested funds of `who`.
	fn do_vest(who: T::AccountId) -> DispatchResult {
		let schedules = Vesting::<T>::get(&who).ok_or(Error::<T>::NotVesting)?;

		let (schedules, locked_now) =
			Self::exec_action(schedules.to_vec(), VestingAction::Passive)?;

		Self::write_vesting(&who, schedules)?;
		Self::write_lock(&who, locked_now);

		Ok(())
	}
```

**File:** substrate/frame/vesting/src/lib.rs (L673-698)
```rust
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
```

**File:** substrate/frame/vesting/src/tests.rs (L699-736)
```rust
#[test]
fn merging_shifts_other_schedules_index() {
	// Schedules being merged are filtered out, schedules to the right of any merged
	// schedule shift left and the merged schedule is always last.
	ExtBuilder::default().existential_deposit(ED).build().execute_with(|| {
		let sched0 = VestingInfo::new(
			ED * 10,
			ED, // Vesting over 10 blocks.
			10,
		);
		let sched1 = VestingInfo::new(
			ED * 11,
			ED, // Vesting over 11 blocks.
			11,
		);
		let sched2 = VestingInfo::new(
			ED * 12,
			ED, // Vesting over 12 blocks.
			12,
		);

		// Account 3 starts out with no schedules,
		assert_eq!(VestingStorage::<Test>::get(&3), None);
		// and some usable balance.
		let usable_balance = Balances::usable_balance(&3);
		assert_eq!(usable_balance, 30 * ED);

		let cur_block = 1;
		assert_eq!(System::block_number(), cur_block);

		// Transfer the above 3 schedules to account 3.
		assert_ok!(Vesting::vested_transfer(Some(4).into(), 3, sched0));
		assert_ok!(Vesting::vested_transfer(Some(4).into(), 3, sched1));
		assert_ok!(Vesting::vested_transfer(Some(4).into(), 3, sched2));

		// With no schedules vested or merged they are in the order they are created
		assert_eq!(VestingStorage::<Test>::get(&3).unwrap(), vec![sched0, sched1, sched2]);
		// and the usable balance has not changed.
```
