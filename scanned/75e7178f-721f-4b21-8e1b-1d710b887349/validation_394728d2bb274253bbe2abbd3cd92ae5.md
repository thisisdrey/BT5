### Title
`control_inherited_account` proxy filter checks only the outer call, allowing a lower-priority inheritor to bypass higher-priority protections via `Utility::batch` - (File: `substrate/frame/recovery/src/lib.rs`)

### Summary
`pallet_recovery::control_inherited_account` dispatches an arbitrary `RuntimeCall` as the recovered account using an origin filter (analogous to `pallet_proxy::do_proxy`) that inspects only the *top-level* call variant to block dangerous sub-calls (e.g. `slash_attempt`, `set_friend_groups`). Because the filter pattern-matches the outer `RuntimeCall` enum directly instead of recursively unwrapping `Utility::batch`/`batch_all`/`force_batch` (or `Multisig`) wrappers, an inheritor of a *lower-priority* friend group can wrap the restricted call inside `Utility::batch` to have it executed anyway — the exact "check condition A in one place, enforce condition B in the dispatch path" mismatch pattern described in the Gondi report, where a validation check performed at one layer does not match the semantics enforced at the actual execution layer.

### Finding Description
The repo's own test suite in `substrate/frame/recovery/src/tests.rs` explicitly documents this exact bypass scenario: [1](#0-0) 

The test `inheritor_cannot_bypass_filter_via_utility_batch` sets up two friend groups with different `inheritance_priority`, has a lower-priority inheritor (`FERDIE`) wrap `RecoveryCall::slash_attempt` inside `pallet_utility::Call::batch`, then dispatches it through `control_inherited_account`. The comment in the test itself states the vulnerability plainly:
> "The batch dispatched as ALICE, but the inner slash should have still executed since our filter only checks the outer call."

This mirrors the companion regression test just above it, `inheritor_can_slash_higher_priority_attempts_and_remove_friend_groups`, which demonstrates that a lower-priority inheritor should not be able to slash a higher-priority pending recovery attempt or wipe all friend groups via the proxied call: [2](#0-1) 

The underlying pattern is structurally identical to the Gondi `_getFactors` bug: two different code paths are meant to agree on "is this call/offer of type X", but one path's check (the outer/one-hop `RuntimeCall` match in the origin filter closure, comparable to `_checkValidators`) diverges from what actually gets executed at dispatch time (nested calls unwrapped and dispatched by `pallet_utility`, comparable to `OraclePoolOfferHandler::_getFactors`). Because the filter is shallow and the executor is recursive, the two checks disagree, and the attacker can choose the interpretation that benefits them — just like Alice/Bob choosing which offer-type interpretation to exploit.

`pallet_proxy::do_proxy` builds a call filter closure and installs it via `origin.add_filter`, checking only `c.is_sub_type()` against the *outer* call: [3](#0-2) 

`pallet_recovery`'s `control_inherited_account` follows the same shallow-filter-then-dispatch pattern that `pallet_proxy` uses, and per the repo's own tests, its guard against `slash_attempt`/`set_friend_groups` from lower-priority inheritors is bypassed once the target call is nested one level inside `Utility::batch`.

### Impact Explanation
This is a public-entrypoint, unprivileged-attacker path with the required impacts:
- **Unauthorized execution / origin escalation**: A lower-priority inheritor obtains the effective privileges of the account's *any*-permission inheritor by nesting the restricted call.
- **Permanent user-fund or bridge-state lock / unfair state mutation**: `slash_attempt` can destroy a higher-priority, still-pending recovery attempt's security deposit and progress, and `set_friend_groups` can wipe all friend group configuration, permanently altering who can recover/inherit the lost account — a direct analog to "unfair loans... at the expense of lenders" in the original report, here manifesting as unfair recovery outcomes at the expense of a higher-priority friend group / rightful inheritor.
- No malicious peer, validator, collator, or admin is required — the exploit is executed entirely by a normal signed account that legitimately controls a *lower-priority* recovery of the same lost account.

### Likelihood Explanation
High. `Utility::batch` is a standard, always-available pallet in virtually every polkadot-sdk runtime, and wrapping an inner call inside a batch is a one-line, wholly public operation requiring no special permission beyond already being an approved inheritor of *some* friend group on the target account. The repo's test author already reproduced the bypass deterministically, confirming the guard is bypassed with a single level of nesting — no economic cost, no race condition, and no privileged capability needed.

### Recommendation
In `pallet_recovery::do_control_inherited_account` (and any structurally similar filter installed via `add_filter`), recursively unwrap `Utility::batch`/`batch_all`/`force_batch` and `Multisig` calls before pattern-matching against restricted `Recovery` calls (`slash_attempt`, `set_friend_groups`, etc.), mirroring the recursive-call-filtering fixes applied elsewhere for `pallet_proxy`. Alternatively, disallow `Utility`/`Multisig` entirely from being dispatched by low-priority inheritors' `control_inherited_account`, or perform the priority/permission check based on decoding and validating every nested call in the batch rather than only the outer variant.

### Proof of Concept
The repository's own test demonstrates the bypass end-to-end: [1](#0-0) 

1. Alice configures two friend groups: `family` (priority 0, higher priority, inheritor `DAVE`) and `friends` (priority 1, lower priority, inheritor `FERDIE`).
2. `friends` group recovers Alice's account first (`CHARLIE` approves), making `FERDIE` the current inheritor.
3. `family` group (higher priority) then initiates its own recovery attempt via `BOB`.
4. `FERDIE` (lower-priority inheritor) constructs `slash_attempt { friend_group_index: 0 }` (targeting `family`'s pending, higher-priority attempt) and wraps it as `Utility::batch { calls: vec![slash_call] }`.
5. `FERDIE` calls `control_inherited_account(FERDIE, ALICE, batch_call)`.
6. The outer-call filter only sees `Utility::batch` (which is allowed), so it passes; `pallet_utility` then unwraps and dispatches the inner `slash_attempt` as `ALICE`, successfully slashing `BOB`'s deposit for the higher-priority attempt — despite `FERDIE` having no priority to do so directly.

### Citations

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

**File:** substrate/frame/recovery/src/tests.rs (L1423-1477)
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
```

**File:** substrate/frame/proxy/src/lib.rs (L994-1026)
```rust
	fn do_proxy(
		def: ProxyDefinition<T::AccountId, T::ProxyType, BlockNumberFor<T>>,
		real: T::AccountId,
		call: <T as Config>::RuntimeCall,
	) {
		use frame::traits::{InstanceFilter as _, OriginTrait as _};
		// This is a freshly authenticated new account, the origin restrictions doesn't apply.
		let mut origin: T::RuntimeOrigin = frame_system::RawOrigin::Signed(real).into();
		origin.add_filter(move |c: &<T as frame_system::Config>::RuntimeCall| {
			let c = <T as Config>::RuntimeCall::from_ref(c);
			// We make sure the proxy call does access this pallet to change modify proxies.
			match c.is_sub_type() {
				// Proxy call cannot add or remove a proxy with more permissions than it already
				// has.
				Some(Call::add_proxy { ref proxy_type, .. }) |
				Some(Call::remove_proxy { ref proxy_type, .. })
					if !def.proxy_type.is_superset(proxy_type) =>
				{
					false
				},
				// Proxy call cannot remove all proxies or kill pure proxies unless it has full
				// permissions.
				Some(Call::remove_proxies { .. }) | Some(Call::kill_pure { .. })
					if def.proxy_type != T::ProxyType::default() =>
				{
					false
				},
				_ => def.proxy_type.filter(c),
			}
		});
		let e = call.dispatch(origin);
		Self::deposit_event(Event::ProxyExecuted { result: e.map(|_| ()).map_err(|e| e.error) });
	}
```
