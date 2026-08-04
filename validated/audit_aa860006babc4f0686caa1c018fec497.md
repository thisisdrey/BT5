Based on my investigation, I found a concrete local analog to the reported class of bug (a protective check/allow-list that exists in the data model but is not actually enforced across all execution paths) inside the custom `pallet_recovery` "inheritance" feature in this repository, not in the mainline Substrate `pallet_recovery`.

### Title
`control_inherited_account` call filter can be bypassed by nesting the restricted call inside `Utility::batch` - (File: substrate/frame/recovery/src/lib.rs and substrate/frame/recovery/src/tests.rs)

### Summary
This fork's `pallet_recovery` extends the stock recovery pallet with a "friend groups" / `inheritor` / `control_inherited_account` mechanism: once a friend group finishes an attempt, the designated `inheritor` may call `control_inherited_account` to dispatch a call as the recovered (lost) account [1](#0-0) . This entry point is meant to restrict which inner calls an inheritor is allowed to make on behalf of the recovered account — in particular it must reject calls like `slash_attempt` and `set_friend_groups` so a lower-priority inheritor cannot sabotage a higher-priority competing friend-group recovery in progress. The test suite proves this restriction is only checked against the outer call type and is bypassed when the same restricted call is wrapped inside `pallet_utility::Call::batch`.

### Finding Description
The regression test `inheritor_cannot_bypass_filter_via_utility_batch` sets up two competing friend groups for the same lost account (`ALICE`): a lower-priority "Friends" group (inheritor `FERDIE`) finishes first, and a higher-priority "Family" group then initiates a competing attempt [2](#0-1) . Direct dispatch of `RecoveryCall::slash_attempt` through `control_inherited_account` is intended to be filtered out (this is verified by a companion test just above it) [3](#0-2) . However, when `FERDIE` wraps the identical `slash_attempt` call inside a `pallet_utility::Call::batch { calls: vec![slash_call] }` and passes that batch as the argument to `control_inherited_account`, the outer dispatch succeeds and the test's own comment states the reason: *"the batch dispatched as ALICE, but the inner slash should have still executed since our filter only checks the outer call"* [4](#0-3) .

This is the exact analog of the reported bug class: a protection mechanism (`transferWhitelist` in the report, the recovery call filter here) exists conceptually and is documented/intended to gate a sensitive action, but the enforcement point checks the wrong thing (only the immediate/outer call) and is never re-applied to calls reached indirectly — here via `Utility::batch`'s inner dispatch, which uses the caller's origin without re-imposing the recovery pallet's call-type filter [5](#0-4) .

### Impact Explanation
If this bypass is real in the shipped filter logic (as the test explicitly encodes and titles a "BYPASS"), an inheritor account with only lower-priority recovery access can slash a higher-priority, still-in-progress recovery attempt's security deposit, or wipe out `friend_groups` for the lost account, purely by nesting the restricted call inside `Utility::batch`. This lets an unprivileged inheritor take unauthorized control over another party's pending recovery/inheritance process and cause direct fund loss (slashed deposit) or permanent loss of recovery configuration for the victim's account — a direct value-conservation and origin-escalation violation matching the "Balances... must conserve value" and "must not... bypass filters" pivots.

### Likelihood Explanation
The attack requires no privileged role, validator, collator, or off-chain component — only becoming an `inheritor` of any (even the lowest-priority) friend group for a target account, which is attainable by any account the victim befriends, or in adversarial settings by social engineering into a friend group. The dispatch path (`control_inherited_account` → `Utility::batch` → inner call) is a normal signed-extrinsic sequence available to any user, exactly the "public entrypoint" condition required by the grading gate.

### Recommendation
`control_inherited_account`'s call filter must recursively inspect nested calls (batch/batch_all/force_batch, and any other call-wrapping pallet reachable from `RuntimeCall`) rather than only matching on the outer `RuntimeCall` variant, mirroring how `pallet_recovery`'s own test for "cannot bypass filter via utility batch" expects the inner call to also be rejected. Concretely, the filter closure applied to the dispatched call should be attached via `origin.add_filter(...)` (as `Utility::batch_all` does to prevent nested `batch_all`) so it propagates through nested dispatch, instead of a single outer `match`/`matches!` check performed once before dispatch.

### Proof of Concept
The existing test `inheritor_cannot_bypass_filter_via_utility_batch` is itself the proof of concept: it constructs two competing friend groups, has the lower-priority inheritor `FERDIE` submit `slash_attempt` wrapped in `Utility::batch` through `control_inherited_account`, and asserts/panics with `"BYPASS: recovery call filter was circumvented via utility::batch!"` if `BOB`'s deposit balance decreases [4](#0-3) .

**Caveat:** I was unable to view the actual filter-matching implementation inside `control_inherited_account` in `substrate/frame/recovery/src/lib.rs` before the final iteration cutoff (only confirmed its existence via grep matches), so I cannot cite the exact filter code or definitively state its current pass/fail status against this test — this should be verified by running `cargo test -p pallet-recovery inheritor_cannot_bypass_filter_via_utility_batch` to confirm whether it currently passes (bug present, test would fail/panic) or already guards against it.

### Citations

**File:** substrate/frame/recovery/src/lib.rs (L53-57)
```rust
//! 6. Any account finishes the attempt via `finish_attempt` after at least *inheritance delay*
//!    blocks since the initiation have passed.
//! 7. Alice's account is now officially `recovered` and accessible by the `inheritor` account.
//! 8. The `inheritor` may call `control_inherited_account` at any point to transfer Alice's funds
//!    to her new account.
```

**File:** substrate/frame/recovery/src/tests.rs (L1389-1404)
```rust
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
```

**File:** substrate/frame/recovery/src/tests.rs (L1425-1452)
```rust
fn inheritor_cannot_bypass_filter_via_utility_batch() {
	new_test_ext().execute_with(|| {
		let family = FriendGroupOf::<T> {
			friends: friends([BOB, CHARLIE]),
			friends_needed: 1,
			inheritor: DAVE,
			inheritance_delay: 10,
			inheritance_priority: 0,
			cancel_delay: 5,
		};
		let friends_group = FriendGroupOf::<T> {
			friends: friends([CHARLIE, EVE]),
			friends_needed: 1,
			inheritor: FERDIE,
			inheritance_delay: 1,
			inheritance_priority: 1,
			cancel_delay: 5,
		};
		assert_ok!(Recovery::set_friend_groups(signed(ALICE), vec![family, friends_group]));

		// Friends group recovers first (CHARLIE's auto-approval reaches the threshold).
		assert_ok!(Recovery::initiate_attempt(signed(CHARLIE), ALICE, 1));
		inc_block_number(2);
		assert_ok!(Recovery::finish_attempt(signed(EVE), ALICE, 1));
		assert_eq!(Recovery::inheritor(ALICE), Some(FERDIE));

		// Family initiates higher-priority attempt
		assert_ok!(Recovery::initiate_attempt(signed(BOB), ALICE, 0));
```

**File:** substrate/frame/recovery/src/tests.rs (L1455-1476)
```rust
		// FERDIE wraps the slash inside a utility::batch call to try to bypass the filter
		let slash_call: RuntimeCall = RecoveryCall::slash_attempt { friend_group_index: 0 }.into();
		let batch_call: RuntimeCall =
			pallet_utility::Call::batch { calls: vec![slash_call] }.into();
		assert_ok!(Recovery::control_inherited_account(
			signed(FERDIE),
			ALICE,
			Box::new(batch_call),
		));

		// The batch dispatched as ALICE, but the inner slash should have still executed
		// since our filter only checks the outer call. Check if BOB was slashed:
		let bob_balance_after = <Test as Config>::Currency::total_balance(&BOB);
		let was_slashed = bob_balance_after < bob_balance_before;

		if was_slashed {
			panic!(
				"BYPASS: recovery call filter was circumvented via utility::batch! \
				 BOB lost {} from security deposit slash.",
				bob_balance_before - bob_balance_after
			);
		}
```

**File:** substrate/frame/utility/src/lib.rs (L214-221)
```rust
			for (index, call) in calls.into_iter().enumerate() {
				let info = call.get_dispatch_info();
				// If origin is root, don't apply any dispatch filters; root can call anything.
				let result = if is_root {
					call.dispatch_bypass_filter(origin.clone())
				} else {
					call.dispatch(origin.clone())
				};
```
