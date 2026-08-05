Based on my investigation, I found a strong local analog to the Hats "stale linked request survives unlink" bug class in the custom `pallet-recovery` friend-group/attempt model.

### Title
`revoke_inheritor` does not clear stale, higher-priority `Attempt`s left by a friend group that already lost the inheritance race, letting a discarded friend group take over the account later - (File: `substrate/frame/recovery/src/lib.rs`)

### Summary
Just like Hats' `unlinkTopHatFromTree()` failing to purge `linkedTreeRequests` (leaving a dormant "link request" that a former admin can later approve to re-seize control), `pallet-recovery`'s `revoke_inheritor()` only clears the `Inheritor` storage entry and does not clear any still-pending `Attempt` entries for lower-priority friend groups that were "discarded" (per the `AttemptDiscarded` event path in `finish_attempt`) but never explicitly removed from `Attempt` storage.

### Finding Description
In `finish_attempt` [1](#0-0) , when a lower/equal-priority attempt finishes after a higher-priority group already set the `Inheritor`, the code emits `AttemptDiscarded` but the `Attempt` entry itself was already `take()`n at line 804-805, so that specific entry is gone. The real gap is on the other side: `initiate_attempt` [2](#0-1)  only blocks a *new* attempt if its own priority is not strictly higher than the existing inheritor's priority — it does not prevent multiple friend groups' attempts from coexisting in `Attempt` storage while `Inheritor` is unset, nor does `revoke_inheritor` [3](#0-2)  touch the `Attempt` map at all — it only does `Inheritor::<T>::take(&lost)`.

Concretely: Alice (the lost account) can regain control and call `revoke_inheritor` to strip the current inheritor, believing she has fully "unlinked" from that friend group's takeover. However, if a *different*, lower-priority friend group had already started (or fully approved) a still-outstanding `Attempt` before/while the higher-priority group finished, that `Attempt` entry is untouched by `revoke_inheritor`. Since `Attempt` storage is keyed by `(lost, friend_group_index)` and is independent of `Inheritor`, that stale, fully (or partially) approved attempt remains live. Once `Inheritor` is cleared, `finish_attempt` for that stale attempt can be called again by "anyone who is willing to pay for the inheritor deposit" [4](#0-3) , and since `Inheritor::<T>::get(&lost)` is now `None`, it hits the `None => { ... Inheritor::<T>::insert(...) }` branch [5](#0-4)  unconditionally — reinstating an inheritor Alice never re-approved, exactly mirroring the Hats pattern where a stale request silently survives the "unlink" and is later approved by the original beneficiary.

The only guard against modifying friend groups while attempts are ongoing is in `set_friend_groups` [6](#0-5) , which checks `Attempt::<T>::iter_prefix(&lost)`. But `revoke_inheritor` has no analogous check — it does not call `iter_prefix` or otherwise assert `Attempt` is empty before considering the account "safe" again.

### Impact Explanation
This allows unauthorized origin escalation / account takeover: an account owner who believes they cleaned up a malicious or unwanted recovery relationship via `revoke_inheritor` can have control silently handed back to a friend group whose attempt was never explicitly cancelled or slashed, without any new approval action. This falls squarely under "runtime bugs that compromise intended behavior" and "unauthorized execution or origin escalation" in the impact gate, and requires no malicious peer/validator/governance — only an unprivileged friend group member who initiated an attempt earlier and a permissionless caller of `finish_attempt`.

### Likelihood Explanation
Moderate-to-high: it requires two friend groups with different priorities and timing where a lower-priority attempt remains pending/approved when the account owner revokes the inheritor, but no privileged actor or race condition against block production is needed — any user can trigger `finish_attempt` since it is unrestricted ("Can be called by anyone who is willing to pay for the inheritor deposit").

### Recommendation
`revoke_inheritor` should also purge (or the runtime should otherwise invalidate) any remaining `Attempt` entries for the `lost` account, analogous to how `unlinkTopHatFromTree()` should `delete linkedTreeRequests[...]`. Concretely, iterate and remove all `Attempt::<T>` entries under prefix `lost` (returning deposits/tickets as `cancel_attempt` does) as part of `revoke_inheritor`, or require `Attempt::<T>::iter_prefix(&lost).next().is_none()` before allowing `revoke_inheritor` to succeed, forcing the owner to explicitly cancel/slash all pending attempts first.

### Proof of Concept
1. Alice configures two friend groups via `set_friend_groups`: Group 0 (priority 0, high) and Group 1 (priority 1, low).
2. Group 1's friend calls `initiate_attempt(lost=Alice, friend_group_index=1)` and gets enough `approve_attempt` votes to satisfy `friends_needed`, but waits (does not yet call `finish_attempt`) — the `Attempt` entry for index 1 sits fully approved in storage.
3. Group 0's friend calls `initiate_attempt`/`approve_attempt`/`finish_attempt` for index 0 first; `Inheritor` becomes `(priority=0, inheritor=Group0Inheritor)`.
4. Alice notices Group 0 took over, and — since she still can sign — calls `revoke_inheritor()`, clearing `Inheritor`.
5. Anyone calls `finish_attempt(lost=Alice, friend_group_index=1)` for the still-pending, fully approved Group 1 attempt. Since `Inheritor::<T>::get(&lost)` is now `None`, this succeeds unconditionally and sets `Inheritor` to Group 1's inheritor — reinstating control that Alice never approved and had no visibility into, purely because the stale `Attempt` entry was never cleared by `revoke_inheritor`.

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

**File:** substrate/frame/recovery/src/lib.rs (L695-711)
```rust
			if Self::attempt_of(&lost, friend_group_index).is_ok() {
				return Err(Error::<T>::AlreadyInitiated.into());
			}

			let friend_group = Self::friend_group_of(&lost, friend_group_index)?;
			let initiator_index = friend_group
				.friends
				.iter()
				.position(|f| f == &initiator)
				.ok_or(Error::<T>::NotFriend)?;

			if let Some((inheritance_priority, _, _)) = Inheritor::<T>::get(&lost) {
				ensure!(
					friend_group.inheritance_priority < inheritance_priority,
					Error::<T>::HigherPriorityRecovered
				);
			}
```

**File:** substrate/frame/recovery/src/lib.rs (L790-805)
```rust
		/// Finish a recovery attempt and make the lost account accessible from the inheritor.
		///
		/// Can be called by anyone who is willing to pay for the inheritor deposit.
		#[pallet::call_index(5)]
		#[pallet::weight(T::WeightInfo::finish_attempt())]
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
```

**File:** substrate/frame/recovery/src/lib.rs (L838-871)
```rust
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
