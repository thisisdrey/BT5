Audit Report

## Title
`revoke_inheritor` fails to purge stale, still-pending `Attempt` entries from lower-priority friend groups, allowing an unapproved inheritor takeover after the lost account believed it had cleared recovery state - (File: `substrate/frame/recovery/src/lib.rs`)

## Summary
`revoke_inheritor` only removes the `Inheritor::<T>` storage entry for the calling account and does not touch the `Attempt::<T>` map, unlike `set_friend_groups` which explicitly checks `Attempt::<T>::iter_prefix(&lost)` before allowing changes. Because `finish_attempt` decides how to treat a completed attempt purely by inspecting `Inheritor::<T>::get(&lost)` at call time, a stale fully-approved `Attempt` from a lower-priority friend group — left untouched because a higher-priority group finished first — can be finalized *after* the account owner revokes the higher-priority inheritor, silently reinstating a takeover the owner never approved.

## Finding Description
`initiate_attempt` only blocks starting a new attempt if `Inheritor::<T>::get(&lost)` already holds an entry with priority `<=` the new group's priority; it never inspects or removes other `Attempt` entries already in storage. [1](#0-0) 

When multiple friend groups race, `finish_attempt` for the lower-priority group emits `AttemptDiscarded` and leaves the current inheritor intact if `Inheritor::<T>::get(&lost)` currently shows a strictly-higher-or-equal-priority holder — but this decision is only made if `finish_attempt` is actually called at that time. [2](#0-1)  If the lower-priority group's attempt has already collected enough approvals but `finish_attempt` has simply not yet been called, the `Attempt` entry sits in storage, fully approved and untouched, independent of what happens to `Inheritor`.

`revoke_inheritor` clears only `Inheritor::<T>`: [3](#0-2)  it never calls `Attempt::<T>::iter_prefix(&lost)` or otherwise cancels pending attempts, unlike `set_friend_groups`, which explicitly guards against modifying friend-group config while attempts are pending: [4](#0-3) 

After revocation, anyone can call `finish_attempt` for the stale entry. Since `Inheritor::<T>::get(&lost)` now returns `None`, execution falls into the `None` branch and unconditionally installs the stale group's inheritor: [5](#0-4)  `finish_attempt` is explicitly documented as callable by anyone willing to pay the inheritor deposit, with no requirement of being a friend, the lost account, or holding any special origin.

This reproduces exactly: the account owner performs an action (`revoke_inheritor`) intended to fully clear recovery state and regain sole control, but a previously-approved, independent recovery path resurfaces and succeeds without any new consent from the owner, because the storage entry it depends on (`Attempt`) was never invalidated by the "unlink" action.

## Impact Explanation
This is unauthorized origin escalation: control over the lost account's assets/identity can be handed to an inheritor the account owner explicitly tried to revoke recovery access from, without any new friend approvals or owner consent. It corrupts the `Inheritor::<T>` value for the lost account with a stale, previously-discardable inheritor entry, in violation of the invariant that `revoke_inheritor` should return the account to an unrecoverable-by-old-config state. This matches the impact-gate category "runtime bugs that compromise intended behavior" / "unauthorized execution or origin escalation."

## Likelihood Explanation
Exploitability requires only: (1) two friend groups with different priorities configured via `set_friend_groups`, (2) the lower-priority group's attempt reaching full approval before or concurrently with the higher-priority group's `finish_attempt`, and (3) the owner calling `revoke_inheritor` without separately cancelling the lower-priority attempt. All required actions (`initiate_attempt`, `approve_attempt`, `finish_attempt`, `revoke_inheritor`) are permissionless public extrinsics reachable by any account; no validator, governance, or privileged role is needed, and no race against block production is required — only ordinary transaction sequencing. The main mitigating factor is that it requires a specific multi-friend-group setup and timing, but this is a normal, foreseeable recovery configuration, not a contrived edge case.

## Recommendation
`revoke_inheritor` should also purge any remaining `Attempt::<T>` entries for the `lost` account (returning locked deposits/tickets, mirroring `cancel_attempt`'s cleanup), or it should require that `Attempt::<T>::iter_prefix(&lost).next().is_none()` before succeeding, forcing the owner to explicitly cancel/slash all pending attempts first — consistent with the existing guard already used in `set_friend_groups`.

## Proof of Concept
1. Alice calls `set_friend_groups` with Group 0 (`inheritance_priority = 0`) and Group 1 (`inheritance_priority = 1`).
2. A friend in Group 1 calls `initiate_attempt(lost=Alice, friend_group_index=1)` and collects enough `approve_attempt` votes to reach `friends_needed`; the attempt is not yet finished, so `Attempt::<T>::get(Alice, 1)` remains fully approved in storage.
3. A friend in Group 0 completes `initiate_attempt`/`approve_attempt`/`finish_attempt` for index 0; `Inheritor::<T>::get(Alice)` becomes `(0, Group0Inheritor, ticket)`.
4. Alice regains signing capability and calls `revoke_inheritor()`; `Inheritor::<T>::take(&Alice)` succeeds and clears the entry, but `Attempt::<T>::get(Alice, 1)` is untouched.
5. Any account calls `finish_attempt(lost=Alice, friend_group_index=1)`. Since `Inheritor::<T>::get(&Alice)` is `None`, the `None` branch executes unconditionally, inserting `(1, Group1Inheritor, ticket)` into `Inheritor::<T>` — reinstating recovery control Alice never re-approved and had explicitly attempted to clear.

### Citations

**File:** substrate/frame/recovery/src/lib.rs (L609-620)
```rust
		pub fn revoke_inheritor(origin: OriginFor<T>) -> DispatchResult {
			let lost = ensure_signed(origin)?;

			let (_priority, _inheritor, ticket) =
				Inheritor::<T>::take(&lost).ok_or(Error::<T>::NoInheritor)?;

			let _: Result<(), DispatchError> = ticket.try_drop().defensive();

			Self::deposit_event(Event::<T>::InheritorRevoked { lost });

			Ok(())
		}
```

**File:** substrate/frame/recovery/src/lib.rs (L638-640)
```rust
			if Attempt::<T>::iter_prefix(&lost).next().is_some() {
				return Err(Error::<T>::HasOngoingAttempts.into());
			}
```

**File:** substrate/frame/recovery/src/lib.rs (L706-711)
```rust
			if let Some((inheritance_priority, _, _)) = Inheritor::<T>::get(&lost) {
				ensure!(
					friend_group.inheritance_priority < inheritance_priority,
					Error::<T>::HigherPriorityRecovered
				);
			}
```

**File:** substrate/frame/recovery/src/lib.rs (L795-871)
```rust
		pub fn finish_attempt(
			origin: OriginFor<T>,
			lost: AccountIdLookupOf<T>,
			friend_group_index: FriendGroupIndex,
		) -> DispatchResult {
			let caller = ensure_signed(origin)?;
			let lost = T::Lookup::lookup(lost)?;
			let now = T::BlockNumberProvider::current_block_number();

			let (attempt, attempts_ticket, deposit) =
				Attempt::<T>::take(&lost, &friend_group_index).ok_or(Error::<T>::NotAttempt)?;

			// We NEVER block a recovery on a buggy initiator account.
			let _: Result<(), DispatchError> = attempts_ticket.try_drop().defensive();
			let _: Result<BalanceOf<T>, DispatchError> = T::Currency::release(
				&HoldReason::SecurityDeposit.into(),
				&attempt.initiator,
				deposit,
				Precision::BestEffort,
			)
			.defensive();

			let friend_group = Self::friend_group_of(&lost, friend_group_index).defensive()?;

			// Check if the attempt is now complete
			let approvals = attempt.approvals.count_ones();
			ensure!(
				// We use >= defensively, but it should be at most ==
				approvals >= friend_group.friends_needed,
				Error::<T>::NotApproved
			);

			let inheritable_at = attempt
				.init_block
				.checked_add(&friend_group.inheritance_delay)
				.ok_or(ArithmeticError::Overflow)?;
			ensure!(now >= inheritable_at, Error::<T>::NotYetInheritable);
			// NOTE: We dont need to check the cancel delay, since enough friends voted and we dont
			// assume fully malicious behavior.

			let inheritor = friend_group.inheritor;
			let inheritance_priority = friend_group.inheritance_priority;

			match Inheritor::<T>::get(&lost) {
				None => {
					let ticket = Self::inheritor_ticket(&caller)?;
					Inheritor::<T>::insert(&lost, (inheritance_priority, &inheritor, ticket));
					Self::deposit_event(Event::<T>::AttemptFinished {
						lost,
						friend_group_index,
						inheritor,
						previous_inheritor: None,
					});
				},
				// new recovery has a higher priority, we replace the existing inheritor
				Some((old_priority, old_inheritor, ticket))
					if inheritance_priority < old_priority =>
				{
					let ticket = ticket.update(&caller, Self::inheritor_footprint())?;
					Inheritor::<T>::insert(&lost, (inheritance_priority, &inheritor, ticket));
					Self::deposit_event(Event::<T>::AttemptFinished {
						lost,
						friend_group_index,
						inheritor,
						previous_inheritor: Some(old_inheritor),
					});
				},
				Some((_, existing_inheritor, _)) => {
					// The existing inheritor stays since an equal or higher priority group
					// already recovered the account.
					Self::deposit_event(Event::<T>::AttemptDiscarded {
						lost,
						friend_group_index,
						existing_inheritor,
					});
				},
			};
```
