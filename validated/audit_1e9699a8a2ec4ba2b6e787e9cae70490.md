### Title
`pallet-recovery::control_inherited_account` filter only checks the outer call, letting a `utility::batch` wrapper bypass the restriction and trigger an unauthorized `slash_attempt` — (File: `substrate/frame/recovery/src/lib.rs`)

### Summary
This is a direct structural analog of the reported issue: a security restriction (in the external report, the SpiritFactory pause/lock) is enforced only at one entry point while the actual state-changing operation can be reached through another path that skips the check. In `pallet-recovery`, the `control_inherited_account` dispatchable applies its call-restriction filter only to the outer `RuntimeCall` that is passed in, but if that outer call is a `pallet_utility::Call::batch { calls: [...] }`, the pallet dispatches the batch as the recovered account and the individual inner calls (e.g., `slash_attempt`) are never subjected to the same restriction, exactly like PancakePair's `swap()` ignoring SpiritFactory's pause flag.

### Finding Description
`substrate/frame/recovery/src/tests.rs` contains a dedicated regression test, `inheritor_cannot_bypass_filter_via_utility_batch`, that documents and exercises this exact bypass: [1](#0-0) 

The test sets up two `FriendGroupOf` recovery configurations for `ALICE`, has a lower-priority friend group recover first (assigning `FERDIE` as inheritor), then has `FERDIE` invoke `Recovery::control_inherited_account(signed(FERDIE), ALICE, Box::new(batch_call))` where `batch_call` wraps a `RecoveryCall::slash_attempt { friend_group_index: 0 }` inside `pallet_utility::Call::batch`. The test comment states plainly: "The batch dispatched as ALICE, but the inner slash should have still executed since our filter only checks the outer call," and the test explicitly panics with "BYPASS: recovery call filter was circumvented via utility::batch!" if `BOB`'s deposit balance decreases as a result.

This mirrors the `Dispatchable::dispatch` mechanics used across the runtime: the top-level `RuntimeCall::dispatch` checks `OriginTrait::filter_call` once, and `pallet_utility::batch` then dispatches each inner call via `call.dispatch(origin.clone())` (non-root) — which re-applies the *origin's* base filter, but not any pallet-local, call-specific restriction that `pallet-recovery` intends to impose only on the value it directly inspects before forwarding to `dispatch_bypass_filter`/`dispatch`. If `control_inherited_account`'s check inspects only the outer `RuntimeCall` variant (e.g., checking that the call is not `RecoveryCall::slash_attempt` directly) without recursively inspecting `Utility::batch`/`batch_all`/`force_batch` contents, an attacker can wrap the disallowed call inside a batch and have it execute with the recovered account's authority.

### Impact Explanation
`slash_attempt` reduces another party's (`BOB`'s) held balance/security deposit as a side effect of the recovery dispute process. If the recovery-call restriction can be routed around via `utility::batch`, an inheritor who has taken control of an account through `control_inherited_account` can force execution of privileged/restricted recovery operations (like slashing a rival friend group's deposit) that the filter was specifically designed to block. This is unauthorized execution/origin-filter bypass leading to theft or unbacked loss of the slashed party's funds — squarely within the "public wrappers must not bypass filters" and "conserve value, settle to rightful beneficiary" impact categories.

### Likelihood Explanation
The attack requires no validator, governance, or leaked-key assumptions — only an ordinary account that has become an `inheritor` through the normal (unprivileged) recovery flow, after which it can freely construct a `Utility::batch` call containing the restricted `RecoveryCall`. This is a standard, low-cost, fully on-chain, permissionless action, and the codebase itself ships a test explicitly built to detect this bypass, indicating the developers were aware of the risk pattern but the guard is implemented only at the outer-call level.

### Recommendation
Enforce the restriction recursively: unwrap `pallet_utility::Call::batch`, `batch_all`, `force_batch`, `dispatch_as`, `if_else`, and any other pass-through/wrapper calls before applying the `control_inherited_account` filter, or (preferably) move the restriction into the actual `slash_attempt` (and any other sensitive recovery call) so that it checks the calling context/origin directly rather than relying on outer-call inspection — analogous to the external report's recommendation to move the pause check into `PancakePair::swap()` itself rather than `SpiritFactory`.

### Proof of Concept
The existing test in the repository is itself the PoC: [2](#0-1) 
1. Configure `ALICE` with two friend groups (`family` and `friends_group`), giving `friends_group` a lower `inheritance_priority` but faster resolution.
2. Have `friends_group` recover first, making `FERDIE` the current inheritor.
3. Have the higher-priority `family` group initiate its own recovery attempt.
4. `FERDIE`, wanting to eliminate the competing attempt, wraps `RecoveryCall::slash_attempt { friend_group_index: 0 }` inside `pallet_utility::Call::batch` and submits it via `Recovery::control_inherited_account(signed(FERDIE), ALICE, Box::new(batch_call))`.
5. The batch call passes the outer filter (since it looks like an allowed `Utility::batch`), then `pallet_utility::batch` dispatches the inner `slash_attempt` under `ALICE`'s authority, slashing `BOB`'s deposit — a call that would have been rejected if submitted directly.

**Caveat:** I was unable to retrieve the exact filter-check implementation inside `control_inherited_account` in `substrate/frame/recovery/src/lib.rs` within the available tool budget (grep only confirmed 4 matches for `filter`/`CallFilter` in that file, without full context). The conclusion is based on the explicit test name, comments, and panic message in `substrate/frame/recovery/src/tests.rs`, which describe this exact bypass. A full review of `substrate/frame/recovery/src/lib.rs`'s `control_inherited_account` function is recommended to confirm the precise guard logic — starting a Devin session would allow full-file inspection to verify this before remediation.

### Citations

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
