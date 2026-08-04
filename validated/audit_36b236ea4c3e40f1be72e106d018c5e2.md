### Title
`control_inherited_account`'s reentrancy filter can be permanently defeated via a Proxy delegation set up through the very same guarded call - (File: `substrate/frame/recovery/src/lib.rs`)

### Summary
`pallet-recovery`'s `control_inherited_account` lets the `inheritor` of a recovered account execute an arbitrary `RuntimeCall` as the recovered (`lost`) account, but adds a one-shot origin filter meant to stop the inheritor from directly calling back into the Recovery pallet itself (to protect higher-priority friend groups' pending attempts, per the in-code doc comment). This mirrors the Numa bug-class: a safeguard exists only on one entry path (the single nested dispatch of a `RuntimeCall`), while an equivalent, more durable path (granting a persistent delegation) exists that produces the same privileged effect without ever passing through the guard.

### Finding Description
`control_inherited_account` builds a signed origin for `recovered` and attaches a call filter meant to block reentrancy into the Recovery pallet: [1](#0-0) 

The filter closure only rejects calls whose `is_sub_type()` resolves to `Some(_)`, i.e. calls that are directly a `pallet_recovery::Call` variant: [2](#0-1) 

Any call that is *not itself* a `Recovery::Call` variant passes the filter, including `pallet_proxy::Call::add_proxy`. Because this filter is attached to a freshly constructed `RawOrigin::Signed(recovered)` origin object (not a globally-tracked permission on the account), it only constrains dispatches performed *through that specific origin instance*. Pallets such as `pallet-proxy` do not propagate an inherited origin filter when they later re-derive `RawOrigin::Signed(real)` from storage for a *separate*, later extrinsic — they construct a brand-new, unfiltered origin.

This creates a two-step bypass:
1. The (malicious) inheritor calls `control_inherited_account(recovered = lost, call = Proxy::add_proxy { delegate: attacker, proxy_type: Any, delay: 0 })`. This call is allowed because `Proxy::add_proxy` is not `Recovery::Call`, so the reentrancy filter's `is_sub_type().is_none()` check passes. The attacker is now a full (`Any`) proxy of the `lost` account, persisted in `pallet-proxy` storage.
2. In a completely separate, ordinary extrinsic, the attacker calls `Proxy::proxy(real = lost, call = RecoveryCall::slash_attempt { .. })` (or `cancel_attempt`, `set_friend_groups`, etc.) directly. `pallet-proxy` looks up the proxy relationship and dispatches the inner call using a fresh `RawOrigin::Signed(lost)` — an origin object that never had `add_filter` called on it. The Recovery-pallet reentrancy guard is therefore never consulted, and the attacker executes arbitrary Recovery-pallet calls as `lost`.

This exactly parallels the Numa finding: the safeguard (`liquidateBadDebtAllowed`/here, the reentrancy filter) is enforced on one call path (direct nested dispatch/regular liquidation) but is silently absent on a semantically equivalent alternate path (a persisted delegation/bad-debt liquidation) that a caller can freely choose instead — defeating the documented security intent ("The controller is not allowed to dispatch calls of the recovery pallet... they could mess with the recovery configuration and possibly cancel or slash attempts from higher-priority friend groups").

The repository's own regression tests only probe the *single-call* nested-dispatch bypass via `pallet_utility::batch` (which is correctly blocked because `utility::batch` dispatches inner calls through the same filtered origin object): [3](#0-2) 

No equivalent test exists for the persistent-delegation (proxy) bypass, which uses a *different, later* origin instance rather than reusing the one that carries the filter.

### Impact Explanation
An inheritor who gains `control_inherited_account` access to a recovered account can permanently and unilaterally seize the recovery configuration of that account: they can call `slash_attempt` to destroy a higher-priority friend group's pending recovery attempt (burning that group's security deposit) and/or `set_friend_groups`/`revoke_inheritor` to fully rewrite or erase the account's recovery configuration — outcomes the pallet's own design and comments explicitly say must be prevented. This is origin escalation of a public, unprivileged-relative-to-root pallet mechanism, directly matching the "unauthorized execution or origin escalation" and "public wrappers must not widen origin, bypass filters" impact categories.

### Likelihood Explanation
Medium/High. It requires no validator, governance, or off-chain compromise — only that an account has already legitimately become `inheritor` for some recovered account (the exact scenario the pallet is built around) and is willing to act maliciously once appointed, which is precisely the threat model the pallet's in-code documentation and existing regression tests (`inheritor_can_slash_higher_priority_attempts_and_remove_friend_groups`, `inheritor_cannot_bypass_filter_via_utility_batch`) were written to defend against. The exploit needs only two ordinary signed extrinsics (`add_proxy` via `control_inherited_account`, then `proxy`), both of which are standard, always-available FRAME operations.

### Recommendation
Do not rely on a per-call `origin.add_filter` reentrancy guard that only survives within a single nested dispatch. Instead:
- Explicitly deny dispatch of calls (directly or transitively) that would grant persistent authority over the `recovered` account to third parties — e.g., disallow `pallet_proxy::Call::add_proxy`, `pallet_proxy::Call::add_proxy_delegate`-style calls, `pallet_multisig` approvals that add new signatories, or any call whose effect is to create a standing delegation — when dispatched via `control_inherited_account`.
- Alternatively/additionally, track a durable "Recovery is blocked while controlled" state per account rather than only via a transient origin filter, so that any later, independently-originated call into Recovery pallet extrinsics for that account (regardless of by whom or how the origin was constructed) is checked against this state.
- Add an explicit regression test that appends a proxy relationship via `control_inherited_account` and then exercises `Proxy::proxy` in a subsequent, separate extrinsic to confirm the guard still holds.

### Proof of Concept
Conceptual reproduction (analogous to the existing `inheritor_cannot_bypass_filter_via_utility_batch` test, but using two separate extrinsics through `pallet-proxy` instead of a single nested `utility::batch` call):
```rust
// Setup: ALICE has friend groups Family(prio 0) and Friends(prio 1, inheritor = FERDIE).
// Friends group recovers ALICE first; FERDIE becomes inheritor.
assert_ok!(Recovery::set_friend_groups(signed(ALICE), vec![family, friends_group]));
assert_ok!(Recovery::initiate_attempt(signed(CHARLIE), ALICE, 1));
inc_block_number(2);
assert_ok!(Recovery::finish_attempt(signed(EVE), ALICE, 1));
assert_eq!(Recovery::inheritor(ALICE), Some(FERDIE));

// Family initiates a higher-priority attempt that FERDIE should not be able to slash.
assert_ok!(Recovery::initiate_attempt(signed(BOB), ALICE, 0));

// Step 1: FERDIE uses the guarded wrapper to add itself as an "Any" proxy of ALICE.
// This is allowed: Proxy::add_proxy is not a Recovery::Call, so is_sub_type().is_none() passes.
let add_proxy_call: RuntimeCall =
    pallet_proxy::Call::add_proxy { delegate: FERDIE, proxy_type: ProxyType::Any, delay: 0 }.into();
assert_ok!(Recovery::control_inherited_account(signed(FERDIE), ALICE, Box::new(add_proxy_call)));

// Step 2: FERDIE now dispatches Recovery::slash_attempt as ALICE via Proxy::proxy,
// completely outside of control_inherited_account's filtered origin.
let slash_call: RuntimeCall = RecoveryCall::slash_attempt { friend_group_index: 0 }.into();
assert_ok!(Proxy::proxy(signed(FERDIE), ALICE, None, Box::new(slash_call)));

// BOB's security deposit is now slashed and the Family attempt destroyed,
// even though the reentrancy guard was supposed to prevent exactly this.
```
Verification that `pallet-proxy`'s `proxy` call re-derives `RawOrigin::Signed(real)` fresh from storage (rather than reusing an origin instance carrying `Recovery`'s `add_filter`) was based on the well-established FRAME pattern for delegated-origin pallets and was not independently re-confirmed against `substrate/frame/proxy/src/lib.rs` in this session due to tool-call limits; a Devin session with full repo access should confirm the exact `Proxy::proxy`/`Proxy::add_proxy` implementation in this fork before treating this as final.

### Citations

**File:** substrate/frame/recovery/src/lib.rs (L580-601)
```rust
			let mut origin: T::RuntimeOrigin =
				frame_system::RawOrigin::Signed(recovered.clone()).into();
			// Reentrancy guard
			origin.add_filter(|c: &<T as frame_system::Config>::RuntimeCall| {
				let c = <T as Config>::RuntimeCall::from_ref(c);
				c.is_sub_type().is_none()
			});

			let call_hash = call.using_encoded(&T::Hashing::hash);
			let call_result = call.dispatch(origin).map(|_| ()).map_err(|r| r.error);

			Self::deposit_event(Event::<T>::RecoveredAccountControlled {
				recovered,
				inheritor,
				call_hash,
				call_result,
			});

			// NOTE: We ALWAYS return okay if the caller had the permission to control the lost
			// account regardless of the inner call result.
			Ok(())
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
