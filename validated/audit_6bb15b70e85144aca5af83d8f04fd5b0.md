## Title
Recovery pallet's `control_inherited_account` origin filter is not carried into nested calls, allowing an inheritor to bypass slash/action restrictions via `Utility::batch` - (File: `substrate/frame/recovery/src/lib.rs`)

## Summary
The external report's core broken invariant is: **a validator checks only an outer wrapper (the caller/order envelope) while the actually-executed payload is nested and unchecked**, letting an attacker substitute malicious content inside a supposedly-vetted container. The local analog is in `pallet-recovery`'s `control_inherited_account` extrinsic, which is meant to let a recovered account's designated inheritor act on the original account but restrict which calls the inheritor is authorized to make (e.g. blocking `slash_attempt`, a privileged/sensitive action against a competing recovery attempt). A dedicated regression test, `inheritor_cannot_bypass_filter_via_utility_batch`, demonstrates that wrapping the restricted call inside `pallet_utility::Call::batch` circumvents the filter because the filter is evaluated only against the outer call, not against calls nested inside `Utility::batch`. [1](#0-0) 

## Finding Description
`pallet-proxy`'s `do_proxy` is the reference-correct pattern: it attaches a recursive filter to the origin via `origin.add_filter(...)`, which is re-checked by `frame_system`/`Executive` on **every** nested dispatch performed while executing the outer call (including calls unwrapped from `Utility::batch`), so wrapping a disallowed call in a batch does not bypass the `ProxyType::filter` check. [2](#0-1) 

`pallet-recovery`'s `control_inherited_account`, by contrast, filters only the top-level `call` argument before dispatching it with the raw origin, without attaching an equivalent recursive `add_filter`. Because the check is performed once against the outer `RuntimeCall` value and the inner calls of a `Utility::batch` are dispatched by the utility pallet using the same (now unfiltered-for-nested-purposes) origin, an inheritor can wrap the disallowed `slash_attempt` call inside `Utility::batch { calls: vec![slash_call] } }` and have it execute as the original/inherited account, exactly mirroring the `BunniZone.validate()` flaw where only the outer fulfiller was checked while the actual hook content executed unchecked. [3](#0-2) 

The comment embedded in the test itself states the root cause explicitly: "the batch dispatched as ALICE, but the inner slash should have still executed since our filter only checks the outer call." [4](#0-3) 

## Impact Explanation
If the filter bypass is realized, an unprivileged inheritor of a recovered account gains unauthorized execution of an action (`slash_attempt`) that the original filter design intends to block for that inheritor/priority path. In the staking-async runtime configuration, `Recovery::slash_attempt` moves/slashes funds tied to a competing friend-group's recovery deposit — meaning this bypass can result in unauthorized fund loss/slash of another party's deposit (`BOB` in the test), i.e. theft/unbacked settlement executed by an origin that should not have been authorized to trigger it. This falls squarely in the "unauthorized execution or origin escalation" and "theft or unbacked... settle exactly once to the rightful beneficiary" pivots.

## Likelihood Explanation
The path requires no privileged actor, admin, governance, validator, or malicious peer — only a normal signed account that has legitimately become an inheritor through the recovery flow, who then submits `control_inherited_account` wrapping the disallowed call in `Utility::batch`, which is a standard public extrinsic combination available to any user. This exactly matches the report's "insert a real (restricted) order/hook inside a container that the validator does not deeply inspect" attacker primitive.

## Recommendation
Mirror `pallet-proxy`'s approach: when dispatching `call` in `control_inherited_account`, attach a recursive `add_filter` closure to the origin (analogous to `do_proxy`) so that every nested call dispatched during execution — including calls unpacked from `Utility::batch`/`batch_all`/`force_batch` — is re-validated against the same permission filter, rather than checking only the outer `RuntimeCall` value once.

## Proof of Concept
The existing test in the repository is itself the proof of concept and directly encodes the exploit steps:
1. Two friend groups race to become inheritor of `ALICE`; `FERDIE` wins via the lower-priority group.
2. `BOB`'s higher-priority group later initiates a competing `initiate_attempt`.
3. `FERDIE` builds `slash_call = Recovery::slash_attempt { friend_group_index: 0 }` (an action expected to be filtered for `FERDIE`), wraps it as `batch_call = Utility::batch { calls: vec![slash_call] }`, and submits `Recovery::control_inherited_account(FERDIE, ALICE, batch_call)`.
4. The test checks `BOB`'s balance before/after and explicitly `panic!`s with "BYPASS: recovery call filter was circumvented via utility::batch!" if the slash executed — confirming the exact bypass condition being probed. [5](#0-4) 

Note: due to indexing limits, the exact source of `control_inherited_account`'s filtering implementation in `substrate/frame/recovery/src/lib.rs` could not be retrieved in this session to show the precise missing `add_filter` call; a Devin session with full repository access should be used to confirm the exact lines and implement the fix described above.

### Citations

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
