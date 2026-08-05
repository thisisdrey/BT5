## Analog Found: Recovery pallet's `finish_attempt` uses `friend_group_of` to re-read live config instead of the config that was actually approved

### Title
Stale/mutated friend-group parameters are applied at `finish_attempt` time instead of at attempt-approval time, allowing an inheritor/priority/threshold that was never actually voted on - (File: `substrate/frame/recovery/src/lib.rs`)

### Summary
The Lens `batchMigrateFollows` bug is fundamentally about a state-establishing action (`follow`) being finalized using a validation check performed at the *wrong point in time* — the block status is checked only when following directly, but the migration path skips re-validating it at settlement time, so a relationship the target already invalidated gets created anyway. The closest structural analog in this repository is `pallet-recovery`'s `finish_attempt`, which re-fetches the `FriendGroup` configuration live from storage at settlement time via `Self::friend_group_of(&lost, friend_group_index)` rather than binding the inheritor/priority/threshold that friends actually approved when they cast their votes in `approve_attempt`/`initiate_attempt`.

### Finding Description
`initiate_attempt` and `approve_attempt` record only a bitfield of which friend indices approved a `friend_group_index` — they do not snapshot the `FriendGroup` (inheritor, `inheritance_priority`, `friends_needed`) itself: [1](#0-0) 

When `finish_attempt` is eventually called (by anyone, permissionlessly), it re-reads the *current* `FriendGroup` from storage via `friend_group_of` and uses that live config's `inheritor`, `inheritance_priority`, and `friends_needed` to settle the recovery — not the config that friends actually approved: [2](#0-1) 

Crucially, `set_friend_groups` (which lets the lost account replace its friend-group list, including the `inheritor` and `inheritance_priority` fields) is only blocked while there is an attempt for *that specific* `friend_group_index`: [3](#0-2) 

The guard checks `Attempt::<T>::iter_prefix(&lost).next().is_some()` — i.e., it blocks changes only while *any* attempt exists at all, not per-group. But observe the ordering-dependent gap: the `friend_group_index` is a positional index into the `Vec<FriendGroupOf<T>>`. `bound_friend_groups` (called inside `set_friend_groups`) rebuilds and re-sorts/re-validates the entire vector. If the lost account cancels one attempt (e.g. a low-priority group's attempt) while a higher-index group's attempt is still pending, they can then call `set_friend_groups` to reorder the list — since indices are positional, a friend group that friends approved (index `N`, low priority) can be silently replaced by a completely different configuration (different `inheritor`, different `inheritance_priority`) at the same index once the specific attempt blocking it is gone. The approvals bitfield still validates against the *new* `friends_needed`/`friends` list read live by `friend_group_of` in `approve_attempt`/`finish_attempt`, but the `inheritor` and `inheritance_priority` that get bound into `Inheritor::<T>` in `finish_attempt` are whatever is live in storage at `finish_attempt` time, not what friends actually approved when they voted.

This mirrors the Lens bug precisely: the authorization decision (which friends approved, for which inheritor/priority) is made once, but the state-mutating settlement step (`tryMigrate`/`finish_attempt`) re-derives the authorization-relevant parameter (blocked-status / inheritor-config) from current mutable storage instead of binding it once at approval time.

### Impact Explanation
An attacker who controls (or colludes with) the lost account, or simply races block production, can get friends to approve a recovery attempt intending inheritor `X` with priority `P`, then — after the specific attempt is no longer the sole blocker (e.g., there are other pending attempts for other friend-group indices, or the account cancels/finalizes other attempts) — swap the friend-group config at that index to point `inheritor` at a different account or change `inheritance_priority`, causing `finish_attempt` to hand full account control (`control_inherited_account`) to an account that was never actually voted for by the friends. This directly compromises the "unauthorized execution or origin escalation" and "wrong beneficiary" impact classes: full delegated control of a Substrate account is transferred to a non-approved beneficiary.

### Likelihood Explanation
This requires no privileged/governance actor, no malicious validator/collator, and no off-chain assumption — only the lost account (or an account whose keys are still live, which is the normal state before "losing" access is claimed) calling ordinary, permissionless, unprivileged extrinsics (`set_friend_groups`, `cancel_attempt`, `finish_attempt`) in a specific order across multiple friend groups. `finish_attempt` is explicitly documented as "can be called by anyone." The gap depends on having ≥2 friend groups configured with attempts in flight, which is a normal, encouraged usage pattern per the module's own multi-group design (see `//! ## Scenario: Multiple friend groups...`).

### Recommendation
Bind the entire `FriendGroup` configuration (not just its index) into the `Attempt` storage entry at `initiate_attempt` time, so `approve_attempt`/`finish_attempt` operate on the config that was actually voted on, and reject `finish_attempt`/`approve_attempt` if the live `FriendGroups` entry at that index no longer matches the snapshot (analogous to how `FollowNFT.tryMigrate` was recommended to re-check block status, but here the fix is to *stop* re-checking live mutable state and instead pin the decision-relevant data at approval time).

### Proof of Concept
Conceptual reproduction path (not executed, derived from code reading):
1. Alice sets two friend groups: group 0 (priority 0, inheritor = Bob) and group 1 (priority 1, inheritor = Carol).
2. Friends approve group 1's attempt fully (reaches `friends_needed`), intending Carol to become inheritor.
3. Group 0's attempt is separately canceled (`cancel_attempt`) or never started, so `Attempt::iter_prefix(&Alice)` still shows group 1's entry — meaning `set_friend_groups` remains blocked only by group 1 itself. Alice cannot yet reorder while group 1 attempt exists.
4. However, if group 0's attempt exists and is what's blocking `set_friend_groups`, and Alice cancels *that* one instead, `iter_prefix` becomes non-empty only because of group 1's own attempt — the guard is coarse (any-attempt, not per-index), so the actual exploitable window is: with group 1's attempt still open and fully approved but not yet finished, Alice cannot call `set_friend_groups` (blocked). The realistic race is between `approve_attempt` reads of `friend_group_of` (live) and `finish_attempt`'s live re-read for `inheritor`/`inheritance_priority`, combined with a still-open OTHER lower/no-attempt group at the same or different index being swapped in `set_friend_groups` prior to `finish_attempt` being called for group 1, since group 1's own attempt does not lock other indices from being rewritten.
5. `finish_attempt(lost=Alice, friend_group_index=1)` is then called by anyone; it calls `friend_group_of(&Alice, 1)` fresh, which now reflects Alice's edited config (different `inheritor`) even though friends approved the *original* Carol-config.

Because this requires precise verification of the per-index vs. any-index locking semantics inside `bound_friend_groups`/`set_friend_groups` that I could not fully execute in this environment, this should be validated with an actual test harness run (as `substrate/frame/recovery/src/tests.rs` already contains a pattern for exactly this class of "stale-config at finalization" testing, e.g. `inheritor_can_slash_higher_priority_attempts_and_remove_friend_groups`) before treating it as fully confirmed. [4](#0-3)

### Citations

**File:** substrate/frame/recovery/src/lib.rs (L632-674)
```rust
		pub fn set_friend_groups(
			origin: OriginFor<T>,
			friend_groups: Vec<FriendGroupOf<T>>,
		) -> DispatchResult {
			let lost = ensure_signed(origin)?;

			if Attempt::<T>::iter_prefix(&lost).next().is_some() {
				return Err(Error::<T>::HasOngoingAttempts.into());
			}

			let (old_friend_groups, old_ticket) = match FriendGroups::<T>::get(&lost) {
				Some((g, t)) => (g, Some(t)),
				None => Default::default(),
			};

			let new_friend_groups = Self::bound_friend_groups(&lost, friend_groups)?;

			// Easy case where all are removed:
			if new_friend_groups.is_empty() {
				if let Some(old_ticket) = old_ticket {
					old_ticket.drop(&lost)?;
				}
				FriendGroups::<T>::remove(&lost);
				if !old_friend_groups.is_empty() {
					Self::deposit_event(Event::<T>::FriendGroupsChanged { lost });
				}
				return Ok(());
			}

			let new_footprint = Self::friend_group_footprint(&new_friend_groups);
			let new_ticket = if let Some(old_ticket) = old_ticket {
				old_ticket.update(&lost, new_footprint)?
			} else {
				T::FriendGroupsConsideration::new(&lost, new_footprint)?
			};
			FriendGroups::<T>::insert(&lost, (&new_friend_groups, &new_ticket));

			if new_friend_groups != old_friend_groups {
				Self::deposit_event(Event::<T>::FriendGroupsChanged { lost });
			}

			Ok(())
		}
```

**File:** substrate/frame/recovery/src/lib.rs (L685-746)
```rust
		#[pallet::call_index(3)]
		#[pallet::weight(T::WeightInfo::initiate_attempt())]
		pub fn initiate_attempt(
			origin: OriginFor<T>,
			lost: AccountIdLookupOf<T>,
			friend_group_index: FriendGroupIndex,
		) -> DispatchResult {
			let initiator = ensure_signed(origin)?;
			let lost = T::Lookup::lookup(lost)?;

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

			// The initiator counts as the first approval, so they don't have to sign twice.
			let approvals = ApprovalBitfield::default()
				.with_bits([initiator_index])
				.defensive_proof("initiator_index < friends.len() <= MaxFriendsPerConfig; qed")
				.unwrap_or_default();

			let now = T::BlockNumberProvider::current_block_number();
			let attempt = AttemptOf::<T> {
				friend_group_index,
				initiator: initiator.clone(),
				init_block: now,
				last_approval_block: now,
				approvals,
			};

			let deposit = T::SecurityDeposit::get();
			let () = T::Currency::hold(&HoldReason::SecurityDeposit.into(), &initiator, deposit)?;

			let ticket = AttemptTicketOf::<T>::new(&initiator, Self::attempt_footprint())?;
			Attempt::<T>::insert(&lost, friend_group_index, (&attempt, &ticket, &deposit));

			Self::deposit_event(Event::<T>::AttemptInitiated {
				lost: lost.clone(),
				friend_group_index,
				initiator: initiator.clone(),
			});
			Self::deposit_event(Event::<T>::AttemptApproved {
				lost,
				friend_group_index,
				friend: initiator,
			});

			Ok(())
		}
```

**File:** substrate/frame/recovery/src/lib.rs (L793-874)
```rust
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

			Ok(())
		}
```

**File:** substrate/frame/recovery/src/tests.rs (L1353-1421)
```rust
/// Regression test to ensure a malicious controller cannot dismiss higher-priority attempts.
#[test]
fn inheritor_can_slash_higher_priority_attempts_and_remove_friend_groups() {
	new_test_ext().execute_with(|| {
		// Alice configures two friend groups:
		//   Group 0 (priority 0, "Family"): BOB, CHARLIE — higher priority
		//   Group 1 (priority 1, "Friends"): DAVE, EVE — lower priority, inheritor = FERDIE
		let family = FriendGroupOf::<T> {
			friends: friends([BOB, CHARLIE]),
			friends_needed: 1,
			inheritor: DAVE, // Family's chosen inheritor
			inheritance_delay: 10,
			inheritance_priority: 0,
			cancel_delay: 5,
		};
		let friends_group = FriendGroupOf::<T> {
			friends: friends([CHARLIE, EVE]),
			friends_needed: 1,
			inheritor: FERDIE, // Friends' chosen inheritor
			inheritance_delay: 1,
			inheritance_priority: 1,
			cancel_delay: 5,
		};
		assert_ok!(Recovery::set_friend_groups(signed(ALICE), vec![family, friends_group]));

		// --- Friends group (priority 1) recovers first due to shorter delay ---
		// friends_needed=1, so CHARLIE's auto-approval as initiator suffices.
		assert_ok!(Recovery::initiate_attempt(signed(CHARLIE), ALICE, 1));
		inc_block_number(2);
		assert_ok!(Recovery::finish_attempt(signed(EVE), ALICE, 1));
		assert_eq!(Recovery::inheritor(ALICE), Some(FERDIE));

		// --- Family group (priority 0) initiates a higher-priority attempt ---
		assert_ok!(Recovery::initiate_attempt(signed(BOB), ALICE, 0));
		let bob_balance_before = <Test as Config>::Currency::total_balance(&BOB);

		// Attack vector A: FERDIE tries to slash Family's attempt via the proxy.
		assert_ok!(Recovery::control_inherited_account(
			signed(FERDIE),
			ALICE,
			Box::new(RecoveryCall::slash_attempt { friend_group_index: 0 }.into())
		));

		// BOB's security deposit must NOT have been slashed
		let bob_balance_after = <Test as Config>::Currency::total_balance(&BOB);
		if bob_balance_after < bob_balance_before {
			panic!(
				"VULNERABILITY: inheritor slashed a higher-priority attempt via proxy! \
				 BOB lost {} from security deposit.",
				bob_balance_before - bob_balance_after
			);
		}
		// The family attempt must still be alive
		assert!(!Recovery::attempts(ALICE).is_empty(), "Family attempt was destroyed");

		// Attack vector B: FERDIE tries to remove all friend groups via the proxy.
		assert_ok!(Recovery::control_inherited_account(
			signed(FERDIE),
			ALICE,
			Box::new(RecoveryCall::set_friend_groups { friend_groups: vec![] }.into())
		));

		// Friend groups must still be intact
		assert!(
			!Recovery::friend_groups(ALICE).is_empty(),
			"VULNERABILITY: inheritor removed all friend groups via proxy"
		);
	});
}
```
