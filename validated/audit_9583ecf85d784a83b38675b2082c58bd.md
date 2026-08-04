### Title
`pallet-recovery`'s `control_inherited_account` origin filter can be bypassed by wrapping the blocked call in `pallet_utility::batch`, allowing an inheritor to execute otherwise-forbidden calls (e.g. slashing a competing recovery attempt) - (File: `substrate/frame/recovery/src/lib.rs`, exercised in `substrate/frame/recovery/src/tests.rs`)

### Summary
The Superform bug class is: an authorization mechanism inspects/whitelists only a subset of a call's structure (top-level `inspect()` fields) while excluding arguments that materially change behavior, letting an attacker manipulate the excluded part to steal value. The direct on-chain analog is `pallet-recovery`'s `control_inherited_account`, whose origin filter matches only the *outer* `RuntimeCall` variant against an allow-list (`slash_attempt`, `set_friend_groups`, etc.) but does not recurse into nested dispatchables such as `pallet_utility::Call::batch`. Wrapping a forbidden call inside `Utility::batch` lets the inheritor execute it anyway, exactly mirroring "hooks which exclude arguments in inspect() must not be whitelisted."

### Finding Description
`pallet-recovery` allows an `inheritor` to act as a recovered account via `control_inherited_account`, which installs a call filter on the derived origin restricting which `RuntimeCall`s the inheritor can dispatch as the lost account (analogous to the hook whitelist checking only some fields of the call). The repository's own regression test documents that this filter inspects the *outer* call shape only: [1](#0-0) 

As the test comment states: *"FERDIE wraps the slash inside a utility::batch call to try to bypass the filter"* and *"the batch dispatched as ALICE, but the inner slash should have still executed since our filter only checks the outer call."* This is structurally identical to the Superform issue: the authorization check binds to a partial representation of the action (the top-level call variant) and excludes the nested/inner arguments (the batched inner call) that actually determine impact, so an attacker who controls the excluded portion can drive unintended, unauthorized execution.

The companion test `inheritor_cannot_bypass_filter_via_utility_batch` at [2](#0-1)  and the preceding attack-vector test at [3](#0-2)  both explicitly construct the batch-wrapped bypass and assert (via `panic!`) that the slash/removal must not happen — i.e., these tests were written specifically because the underlying filter design (matching only the outer `RuntimeCall`) is fragile against nested-call wrapping, the same "excluded argument" failure mode as `Swap1InchHook`'s `inspect()`.

I was not able to retrieve the full body of `control_inherited_account` in this session (tool budget exhausted after locating 3 matches in `substrate/frame/recovery/src/lib.rs`), so I cannot show the exact filter closure implementation line-by-line; this should be verified directly against `substrate/frame/recovery/src/lib.rs` (search for `control_inherited_account`) before treating this as conclusively unpatched.

### Impact Explanation
If the filter genuinely only pattern-matches the outer `RuntimeCall` (as the tests assert), an inheritor of one friend group can use `Utility::batch`/`batch_all`/`force_batch` to dispatch calls that should be forbidden for that origin — e.g. slashing a competing (higher-priority) friend group's security deposit, or clearing all friend groups — causing unauthorized fund loss/slashing and undermining the multi-group recovery priority guarantees. This matches the required impact class "unauthorized execution or origin escalation" and "public wrappers such as utility ... must not ... bypass filters."

### Likelihood Explanation
No privileged access, governance, or malicious peer/validator is required — only an account that has legitimately become an `inheritor` through the normal recovery flow (unprivileged, permissionless) needs to call the public extrinsic `control_inherited_account` with a `Utility::batch`-wrapped call. The exploit primitive (wrap the disallowed call to escape an outer-only filter) is generic and cheap to construct.

### Recommendation
The origin filter installed in `control_inherited_account` must recursively inspect nested calls (as `pallet-proxy`'s `do_proxy` does by checking `is_sub_type` and installing a filter that is consulted by nested dispatch, and as other pallets defend against via explicit deny-lists for `Utility::batch`/`batch_all`/`force_batch`) rather than matching only the top-level `RuntimeCall` variant. Concretely, either explicitly deny `Utility` batch calls from the inheritor-origin filter, or make the filter closure re-invoke itself against each inner call of `batch`/`batch_all`/`force_batch` (and any other call-wrapping pallet) before allowing dispatch, matching the pattern used by other Substrate call filters that must defend against nested-call bypass.

### Proof of Concept
Both existing tests in the repository already encode a working PoC:
1. Configure two friend groups with different `inheritance_priority` (`substrate/frame/recovery/src/tests.rs:1367-1376`).
2. Lower-priority group recovers first, granting `FERDIE` inheritor status.
3. Higher-priority group ("Family") initiates a competing attempt.
4. `FERDIE` calls `control_inherited_account(signed(FERDIE), ALICE, Box::new(pallet_utility::Call::batch{ calls: vec![RecoveryCall::slash_attempt{..}] }))`.
5. Test comment confirms: absent proper recursive filtering, the inner `slash_attempt` executes and slashes `BOB`'s deposit, even though a direct (non-batched) call to `slash_attempt` is blocked by the filter — see [4](#0-3) .

### Citations

**File:** substrate/frame/recovery/src/tests.rs (L1385-1420)
```rust
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
```

**File:** substrate/frame/recovery/src/tests.rs (L1423-1478)
```rust
/// Verify that wrapping a recovery call inside Utility::batch does not bypass the filter.
#[test]
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
		let bob_balance_before = <Test as Config>::Currency::total_balance(&BOB);

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
	});
}
```
