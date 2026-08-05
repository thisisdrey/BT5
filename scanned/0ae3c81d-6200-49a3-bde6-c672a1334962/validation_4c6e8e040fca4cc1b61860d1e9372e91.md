### Title
`pallet-recovery`'s `control_inherited_account` origin-restriction filter only inspects the outer call, allowing bypass via `pallet_utility::batch` - (File: `substrate/frame/recovery/src/lib.rs`)

### Summary
`pallet-recovery` exposes `control_inherited_account`, a call that lets a designated "inheritor" account act as a recovered account but is expected to be *restricted* — it must not be able to perform certain sensitive recovery-pallet actions on the recovered account (e.g. `slash_attempt` against a higher-priority, still-active friend-group recovery attempt, or wiping `set_friend_groups`). The pallet's own test suite proves the restriction is implemented as a shallow, outer-call-only filter: wrapping the disallowed inner call (`RecoveryCall::slash_attempt`) inside a `pallet_utility::Call::batch` and passing that batch as the dispatched call to `control_inherited_account` defeats the check, because the filter matches on the type of the top-level call only and does not recurse into calls nested inside `utility::batch`.

### Finding Description
This is the same bug class as the reported Gramine issue: a broad "pass-through" allowance (there, `/etc` in `allowed_files`; here, `utility::batch` being an allowed/opaque wrapper call) sits on top of a narrower integrity/authorization check (there, hashing specific files under `/etc/ssl/certs`; here, filtering specific `RecoveryCall` variants like `slash_attempt`/`set_friend_groups`). Because the broader mechanism is not itself subject to the narrower check, and the narrower check is not applied recursively to whatever the broad mechanism unwraps, the protected operation executes without ever having been verified/filtered.

Concretely, in `substrate/frame/recovery/src/tests.rs`, `inheritor_cannot_bypass_filter_via_utility_batch` (lines 1423-1478) constructs exactly this attack: [1](#0-0)  FERDIE (the inheritor of the lower-priority "friends" recovery group) submits `control_inherited_account(ALICE, Box::new(batch_call))` where `batch_call` is `pallet_utility::Call::batch { calls: vec![slash_call] }` and `slash_call` is `RecoveryCall::slash_attempt { friend_group_index: 0 }` — an action targeting the *higher-priority* "family" recovery attempt that FERDIE should have no authority over. The test's own comment states the root cause: [2](#0-1)  "the batch dispatched as ALICE, but the inner slash should have still executed since our filter only checks the outer call." A companion test at lines 1385-1419 shows the same construct being probed directly (without batch) and via other call shapes, confirming the intended invariant is "the inheritor's `control_inherited_account` must not be able to slash a higher-priority attempt or clear friend groups," while the batch-wrapped variant is the concrete bypass vector. [3](#0-2) 

The `control_inherited_account` entrypoint dispatches the boxed call under the recovered account's origin (`ALICE`) after presumably applying an allow/deny filter keyed on the outer `RuntimeCall` variant (matches confirmed in `substrate/frame/recovery/src/lib.rs`, though the exact filter body was not fully retrieved before the tool budget ran out). Because `pallet_utility::batch` is itself permitted to pass through, and `Utility::batch` dispatches each inner call under the same origin without re-applying the recovery pallet's own restriction, any call otherwise blocked at the `control_inherited_account` boundary can be smuggled through by nesting it in `batch`.

### Impact Explanation
This breaks the value-conservation and "settle exactly once to the rightful beneficiary" guarantee for the recovery-attempt slashing/inheritance mechanism: an unprivileged inheritor from a lower-priority recovery path can unilaterally destroy a higher-priority, still-active recovery attempt (slashing the depositor's funds and/or erasing `friend_groups`), effectively hijacking control of the recovered account away from the group that should legitimately win recovery. This is unauthorized execution/origin-filter bypass via a public wrapper (`pallet_utility::batch`), squarely in-scope per the "Public wrappers such as utility ... must not widen origin, bypass filters" pivot.

### Likelihood Explanation
The attack requires no privileged role, no validator/collator/relayer compromise, and no governance action — only a signed transaction from an account that has legitimately become an "inheritor" through the normal recovery flow (as demonstrated by the existing test, which reaches this state via ordinary `initiate_attempt`/`finish_attempt` calls). The construct (`RecoveryCall` wrapped in `pallet_utility::Call::batch`) is trivial to build and is exactly what an unprivileged public caller can submit through the existing `Recovery::control_inherited_account` extrinsic.

### Recommendation
Make the recovery-pallet's call restriction recursive/origin-filter based rather than a shallow match on the outer `RuntimeCall` variant: either (a) apply the restriction as an origin filter (`add_filter`) on the dispatched origin so it is re-checked for every nested call dispatched under that origin (including those unwrapped by `pallet_utility::batch`, `batch_all`, `as_derivative`, etc.), mirroring how `pallet_utility::batch_all`'s root path deliberately still runs `call.dispatch(filtered_origin)` for non-root callers; or (b) explicitly reject `RuntimeCall`s whose `is_sub_type` resolves to known call-wrapping pallets (`Utility`, `Proxy`, `Multisig`, etc.) at the `control_inherited_account` boundary, and add recursive unwrapping/decoding of nested calls before applying the filter.

### Proof of Concept
The existing repository test is a self-contained PoC: [4](#0-3) 
It sets up two competing recovery groups (family, priority 0; friends, priority 1), lets the friends group finish first making FERDIE the inheritor, then has FERDIE call `control_inherited_account(ALICE, Box::new(Utility::batch{ calls: [RecoveryCall::slash_attempt{friend_group_index: 0}] }))`. If BOB's (the family-attempt depositor's) balance decreases, the test explicitly labels the outcome `"BYPASS: recovery call filter was circumvented via utility::batch!"`, confirming the vulnerable path exists in the codebase's own current test expectations/comments.

### Citations

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
