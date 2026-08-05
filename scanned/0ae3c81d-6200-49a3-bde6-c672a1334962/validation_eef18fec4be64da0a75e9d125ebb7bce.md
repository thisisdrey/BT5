Based on my research, I found a concrete local analog of this bug class in `pallet-recovery`'s `control_inherited_account`, which uses an origin-level filter as its sole "approval-program" guard — but a differently-shaped dispatch path (`pallet-multisig`'s `as_multi_threshold_1`) is documented to skip origin filters entirely, exactly mirroring the "clear state program bypasses the approval program" flaw.

### Title
`Recovery::control_inherited_account` reentrancy filter can be bypassed via `Multisig::as_multi_threshold_1`, allowing an inheritor to run privileged Recovery calls as the recovered account - (File: `substrate/frame/recovery/src/lib.rs`)

### Summary
`control_inherited_account` protects against a recovered account's inheritor abusing recovery internals (e.g. slashing a higher-priority attempt, wiping friend groups) by attaching a runtime `add_filter` closure to the derived `Signed(recovered)` origin that rejects any inner call that `is_sub_type()` of the Recovery pallet [1](#0-0) . This filter is only consulted by dispatch paths that go through the normal `Dispatchable::dispatch` → `OriginTrait::filter_call` route [2](#0-1) . The pallet's own tests confirm the filter is correctly re-checked for nested `Utility::batch` calls because `Utility::batch`/`batch_all` call `call.dispatch(origin.clone())` (filtered) rather than `dispatch_bypass_filter` unless the origin is Root [3](#0-2) . However, `pallet-utility`'s own documentation states that `as_multi_threshold_1` in `pallet-multisig` is the designated mechanism to intentionally **not honor account-based filtering** that was set up earlier in the call stack (e.g. by a proxy) [4](#0-3) . This is precisely analogous to the reported bug: the vault (here, `control_inherited_account`'s reentrancy filter) assumes every path into the pallet is gated by its added filter (the "approval program"), while an alternate legitimate dispatch entry point (`as_multi_threshold_1`, the "clear state program") is explicitly designed to skip added filters.

### Finding Description
The corrupted invariant is: *"any call dispatched underneath the origin returned from `control_inherited_account` will be checked against the added filter before it can mutate Recovery state."* This invariant only holds for dispatch call-sites that route through `Dispatchable::dispatch` (filtered). If the inheritor wraps the malicious inner call (e.g. `Recovery::slash_attempt` or `Recovery::set_friend_groups`) inside `Multisig::as_multi_threshold_1`, and that multisig call ends up dispatched via `dispatch_bypass_filter` under the same `Signed(recovered)` origin (as `pallet-utility`'s docs explicitly describe this as the intended behavior for such wrapping calls), the `add_filter` closure added in `control_inherited_account` never runs, so `is_sub_type()` is never evaluated against the wrapped Recovery call. The privileged call executes as the recovered account, exactly as the vault-drain PoC executed a privileged transfer by routing through the clear-state program instead of the approval program that the authorization check assumed was mandatory.

### Impact Explanation
If confirmed, this allows an inheritor of a recovered account to bypass the explicit "reentrancy guard" documented at [5](#0-4)  and perform actions the pallet author explicitly intended to forbid: slashing a higher-priority friend group's security deposit, wiping `FriendGroups` to erase competing inheritance claims, or otherwise manipulating recovery state as the recovered account. This is unauthorized state mutation/fund-slashing achieved purely through an unprivileged, permissionless call sequence available to any account holding an inheritor role — no admin, governance, validator, or malicious peer is required, matching the "unauthorized execution or origin escalation" and "theft or unbacked mint/slash" impact classes in scope.

### Likelihood Explanation
Likelihood is only moderate-to-uncertain from static analysis alone: the exact internal dispatch call used by `as_multi_threshold_1` in this repository's `pallet-multisig` was not directly read in this session (only confirmed to exist via `grep`), so I could not verify line-by-line whether it truly calls `dispatch_bypass_filter` on the wrapped call versus a filtered `dispatch`. The claim rests on the explicit doc-comment in `pallet-utility` asserting this is exactly what `as_multi_threshold_1` is for. Given the existing regression tests in `substrate/frame/recovery/src/tests.rs` (`inheritor_cannot_bypass_filter_via_utility_batch`, `inheritor_can_slash_higher_priority_attempts_and_remove_friend_groups`) explicitly guard against the `Utility::batch` route but do **not** contain any test for the `Multisig::as_multi_threshold_1` route, this path appears untested and unguarded.

### Recommendation
- Short term: add a regression test analogous to `inheritor_cannot_bypass_filter_via_utility_batch` but wrapping the malicious inner call in `Multisig::as_multi_threshold_1` to confirm/deny the bypass. If confirmed, change `control_inherited_account`'s guard from a same-origin `add_filter` reentrancy check to an explicit `T::RuntimeCall::filter` allow-list, or recursively unwrap known "filter-bypassing" wrapper calls before checking `is_sub_type()`, mirroring how `pallet-proxy::do_proxy` explicitly special-cases nested `Call::add_proxy`/`remove_proxies` before falling back to `def.proxy_type.filter(c)` [6](#0-5) .
- Long term: document every dispatch call-site in the runtime that intentionally bypasses `OriginTrait` filters (currently only `as_multi_threshold_1` per its doc comment, plus Root paths in `batch`/`batch_all`/`force_batch`), and require any pallet that installs an `add_filter`-based guard (recovery, proxy) to explicitly test against all such known bypass call-sites, not just `Utility::batch`.

### Proof of Concept
1. Alice sets up friend groups and Ferdie becomes inheritor of Alice's account via the standard recovery flow, as in `substrate/frame/recovery/src/tests.rs` setup helpers [7](#0-6) .
2. Bob's higher-priority friend group initiates a competing recovery attempt (`Recovery::initiate_attempt`).
3. Ferdie constructs `inner_call = RecoveryCall::slash_attempt { friend_group_index: 0 }` (or `set_friend_groups { friend_groups: vec![] }`).
4. Instead of passing `inner_call` directly to `control_inherited_account` (which the existing test at [8](#0-7)  shows is correctly filtered), Ferdie wraps it: `wrapped_call = Multisig::as_multi_threshold_1 { other_signatories: vec![], call: inner_call, ... }`.
5. Ferdie calls `Recovery::control_inherited_account(signed(Ferdie), Alice, Box::new(wrapped_call))`.
6. If `as_multi_threshold_1` dispatches its inner call via `dispatch_bypass_filter` (per the documented intent in `pallet-utility`), the `add_filter` closure at [9](#0-8)  never inspects `inner_call`, and `slash_attempt`/`set_friend_groups` executes as Alice — draining Bob's deposit or erasing competing friend groups, which is precisely the outcome the existing `inheritor_can_slash_higher_priority_attempts_and_remove_friend_groups` regression test was written to prevent for the direct and batch-wrapped cases, but does not cover for the multisig-wrapped case.

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

**File:** substrate/frame/support/procedural/src/construct_runtime/expand/call.rs (L174-182)
```rust
			fn dispatch(self, origin: RuntimeOrigin) -> #scrate::dispatch::DispatchResultWithPostInfo {
				if !<Self::RuntimeOrigin as #scrate::traits::OriginTrait>::filter_call(&origin, &self) {
					return ::core::result::Result::Err(
						#system_path::Error::<#runtime>::CallFiltered.into()
					);
				}

				#scrate::traits::UnfilteredDispatchable::dispatch_bypass_filter(self, origin)
			}
```

**File:** substrate/frame/utility/src/lib.rs (L217-221)
```rust
				let result = if is_root {
					call.dispatch_bypass_filter(origin.clone())
				} else {
					call.dispatch(origin.clone())
				};
```

**File:** substrate/frame/utility/src/lib.rs (L246-249)
```rust
		/// NOTE: If you need to ensure that any account-based filtering is not honored (i.e.
		/// because you expect `proxy` to have been used prior in the call stack and you do not want
		/// the call restrictions to apply to any sub-accounts), then use `as_multi_threshold_1`
		/// in the Multisig pallet instead.
```

**File:** substrate/frame/proxy/src/lib.rs (L1002-1022)
```rust
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
```

**File:** substrate/frame/recovery/src/tests.rs (L1356-1383)
```rust
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
```

**File:** substrate/frame/recovery/src/tests.rs (L1389-1394)
```rust
		// Attack vector A: FERDIE tries to slash Family's attempt via the proxy.
		assert_ok!(Recovery::control_inherited_account(
			signed(FERDIE),
			ALICE,
			Box::new(RecoveryCall::slash_attempt { friend_group_index: 0 }.into())
		));
```
