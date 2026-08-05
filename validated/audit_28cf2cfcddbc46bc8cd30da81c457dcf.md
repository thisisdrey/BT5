Audit Report

## Title
`control_inherited_account`'s reentrancy filter can be permanently defeated via a Proxy delegation set up through the very same guarded call - (File: `substrate/frame/recovery/src/lib.rs`)

## Summary
`Recovery::control_inherited_account` lets an `inheritor` dispatch an arbitrary `RuntimeCall` as the recovered account, guarded only by a per-call origin filter that rejects calls whose type is `pallet_recovery::Call` [1](#0-0) . Because `pallet_proxy::Call::add_proxy` is not itself a `Recovery::Call`, the guard permits the inheritor to grant themselves a persistent `Any` proxy over the recovered account, after which they can invoke `Proxy::proxy` in a wholly separate extrinsic to execute Recovery-pallet calls (e.g. `slash_attempt`) as the recovered account without ever going through the filtered origin.

## Finding Description
The guard in `control_inherited_account` constructs a fresh `RawOrigin::Signed(recovered)` and attaches a filter via `origin.add_filter(...)`, which only blocks calls where `is_sub_type()` resolves to `Some(_)` (i.e., direct `Recovery::Call` variants) [2](#0-1) . This filter is a property of that specific `T::RuntimeOrigin` value produced inside this call; it is not a durable, account-level restriction persisted anywhere in storage. The pallet's own doc comment states the explicit intent: the controller must not be able to dispatch Recovery-pallet calls, "Otherwise they could mess with the recovery configuration and possibly cancel or slash attempts from higher-priority friend groups" [3](#0-2) .

`pallet_proxy::Call::add_proxy` is a distinct pallet's call type, not a sub-type of `pallet_recovery::Call`, so `is_sub_type().is_none()` is `true` and the filter passes it through — the inheritor can use `control_inherited_account` to add themselves as an `Any` proxy of the recovered account, persisting this relationship in `pallet-proxy` storage. A later, independent extrinsic calling `Proxy::proxy` re-derives its dispatch origin from storage for the target account at that later block — a brand-new origin object that never had `add_filter` invoked on it — so the Recovery reentrancy guard is never consulted for that second, separately-originated call. This lets the inheritor execute `slash_attempt`, `set_friend_groups`, `revoke_inheritor`, etc. as the recovered account outside the intended guard.

The repository's existing regression test only checks the single-call nested-dispatch bypass via `pallet_utility::batch`, which correctly fails to bypass the filter because `Utility::batch` dispatches its inner calls through the *same* origin object carrying the filter [4](#0-3) . There is no test covering the two-step, cross-extrinsic proxy-delegation bypass, which uses a distinct, later-constructed origin rather than reusing the filtered one.

## Impact Explanation
This is an origin-escalation vulnerability directly matching the required pivot: "Public wrappers such as `utility`, `proxy`, `multisig`... must not widen origin, bypass filters." An inheritor with legitimate `control_inherited_account` access can permanently and unilaterally seize control of the recovered account's recovery configuration by slashing a higher-priority friend group's pending attempt (destroying that group's security deposit) or by rewriting/erasing the account's friend groups and inheritor entirely — precisely the outcome the pallet's own documentation says the filter must prevent.

## Likelihood Explanation
Medium/High. No privileged, governance, or off-chain compromise is required — only that an account has legitimately become `inheritor` of a recovered account (the pallet's designed normal-operation scenario) and chooses to act maliciously. The exploit requires only two ordinary, always-available signed extrinsics: `control_inherited_account` wrapping `Proxy::add_proxy`, followed by `Proxy::proxy` wrapping the desired Recovery call.

## Recommendation
- Do not rely solely on a transient per-call origin filter for reentrancy protection; explicitly reject calls (directly or transitively) that create persistent delegated authority over the `recovered` account when dispatched through `control_inherited_account` (e.g., block `pallet_proxy::Call::add_proxy`/`add_proxy_delegate`, `pallet_multisig` signatory additions, or any call whose effect grants standing control).
- Alternatively, track a durable "Recovery is under control" flag per account in storage so any later, independently-originated dispatch into Recovery-pallet extrinsics for that account is checked against this persistent state rather than only against a transient origin filter.
- Add a regression test that adds a proxy relationship via `control_inherited_account` and then exercises `Proxy::proxy` in a separate extrinsic to confirm the guard still holds.

## Proof of Concept
1. `ALICE` has `family` (priority 0) and `friends_group` (priority 1, inheritor `FERDIE`) friend groups; `friends_group` recovers first, making `FERDIE` the inheritor.
2. `family` initiates a higher-priority pending recovery attempt.
3. `FERDIE` calls `Recovery::control_inherited_account(signed(FERDIE), ALICE, Box::new(Proxy::add_proxy{ delegate: FERDIE, proxy_type: Any, delay: 0 }))` — passes the filter since `add_proxy` is not a `Recovery::Call`.
4. `FERDIE` then calls `Proxy::proxy(signed(FERDIE), ALICE, None, Box::new(Recovery::slash_attempt{ friend_group_index: 0 }))` in a separate extrinsic — this dispatches as a fresh `RawOrigin::Signed(ALICE)` that never had the Recovery reentrancy filter applied, successfully slashing `family`'s pending attempt.

### Citations

**File:** substrate/frame/recovery/src/lib.rs (L557-561)
```rust
		/// Allows the inheritor of a recovered account to control it.
		///
		/// The controller is not allowed to dispatch calls of the recovery pallet. Otherwise they
		/// could mess with the recovery configuration and possibly cancel or slash attempts from
		/// higher-priority friend groups.
```

**File:** substrate/frame/recovery/src/lib.rs (L580-589)
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
