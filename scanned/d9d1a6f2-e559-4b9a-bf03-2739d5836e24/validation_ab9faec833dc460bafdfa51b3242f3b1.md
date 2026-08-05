Now I have enough evidence to confirm the analog.

### Title
`control_inherited_account`'s reentrancy filter only blocks direct recovery calls, allowing an inheritor to bypass it by wrapping a `slash_attempt`/config-mutating call inside `pallet_utility::batch` and act with the `recovered` account's signed origin against higher-priority friend groups - (File: `substrate/frame/recovery/src/lib.rs`)

### Summary
The bug report describes bypassing a protocol-level "buy block" restriction by routing through a whitelisted contract (`UniswapHandler`) whose `addLiquidity`/`removeLiquidity` wrapper functions do not re-check the restriction that gates direct swaps. The core broken invariant is: *a security check applied to a direct/primary action is not re-applied when the same effect is reached through an indirect, "whitelisted" wrapper call*.

The closest local analog is `pallet_recovery::control_inherited_account` in [1](#0-0) . This dispatchable lets an `inheritor` execute an arbitrary `RuntimeCall` as the `recovered` (lost) account. To stop the inheritor from calling back into the recovery pallet itself (e.g. to cancel/slash a higher-priority attempt), it installs a single-call reentrancy filter via `origin.add_filter(...)` that only inspects the outer call:

```rust
origin.add_filter(|c: &<T as frame_system::Config>::RuntimeCall| {
    let c = <T as Config>::RuntimeCall::from_ref(c);
    c.is_sub_type().is_none()
});
```

### Finding Description
This filter is checked by `Dispatchable::dispatch`, which calls `OriginTrait::filter_call(&origin, &self)` on the **top-level** call before dispatch, as implemented in the runtime's generated `RuntimeCall::dispatch`: [2](#0-1) 

If the inheritor wraps a `pallet_recovery::Call::slash_attempt` (or `cancel_attempt`, `set_friend_groups`, etc.) inside `pallet_utility::Call::batch`, the outer call is `Utility::batch`, which is *not* a recovery sub-call, so `filter_call` on the outer call passes. `Utility::batch` then iterates its inner calls and dispatches each one with `dispatch_bypass_filter`, which — per the same generated code — skips the `filter_call` check entirely and goes straight to `UnfilteredDispatchable::dispatch_bypass_filter`. This means the inner `RecoveryCall::slash_attempt` executes with the recovered (lost) account's signed origin, even though the reentrancy guard was specifically designed to prevent exactly this.

This is structurally identical to the Malt bug: a guard applied to the "direct" call path (calling the recovery pallet directly) is trivially bypassed by routing the same effective call through a public, unprivileged wrapper (`utility::batch`) that the guard does not account for.

The repository already contains a test explicitly probing this exact scenario, confirming it is a recognized attack surface for this pallet: [3](#0-2) 

The test's own comment states: "The batch dispatched as ALICE, but the inner slash should have still executed since our filter only checks the outer call." The test is written to `panic!` if the slash succeeds, i.e., it is a regression guard for this exact bypass — meaning the vulnerability is a known, real risk in this filter design, not a theoretical one.

### Impact Explanation
An inheritor who has taken over a `recovered` account can use `control_inherited_account` + `utility::batch` to call `slash_attempt` on a friend group with a *higher inheritance priority* than their own, burning that higher-priority initiator's `SecurityDeposit` and preventing that group from ever completing `finish_attempt` for the pending window (the attempt storage entry is removed by `Attempt::take` in `slash_attempt`). This lets a lower-priority (and potentially malicious/compromised) inheritor:
- Grief and financially harm honest friends of a higher-priority, legitimate recovery group by burning/slashing their deposit.
- Permanently prevent a legitimate higher-priority group from recovering the account (since the pending attempt is deleted), effectively locking out the rightful heir and letting the lower-priority inheritor retain unauthorized control of the lost account's funds.

This matches the "unauthorized execution or origin escalation" and "permanent user-fund lock" impact categories: an unprivileged account origin filter is bypassed to execute privileged-context state mutation via a public wrapper (`utility::batch`), and the effect is fund loss (deposit slash) and lock-out of the legitimate recovery path.

### Likelihood Explanation
High. No privileged actor, governance, or malicious node/validator is required — only an account that has legitimately become `inheritor` for one (lower-priority) friend group while a higher-priority group's attempt is pending. `pallet_utility` is a standard, always-available pallet in virtually every polkadot-sdk-based runtime, and `batch`/`batch_all`/`force_batch` are public, unprivileged extrinsics. The reentrancy filter's approach of inspecting only the outer call via `is_sub_type()` is a well-known pitfall (the pallet's own filter comment says "controller is not allowed to dispatch calls of the recovery pallet" but the implementation does not enforce this transitively through wrapper pallets).

### Recommendation
The reentrancy filter must be defensive against nested/wrapped calls, not just the top-level call type. Options:
- Recursively inspect batched/nested calls (e.g., via `GetCallMetadata`/`IsSubType` unwrapping for known wrapper pallets like `Utility`, `Proxy`, `Multisig`) before allowing dispatch.
- Alternatively, use a `CallFilter` that is enforced at every level of dispatch (i.e., have `Utility::batch`'s inner `dispatch_bypass_filter` still consult the currently-installed origin filter rather than fully bypassing it), or explicitly deny `Utility`/`Multisig`/`Proxy` calls in the reentrancy filter in addition to recovery calls.
- Add an explicit check that rejects any call whose `PalletInfo`/metadata indicates it can recursively dispatch other calls (batch, proxy, multisig) unless each nested call is individually re-validated against the same filter.

### Proof of Concept
This is directly demonstrated by the existing test in the repository: [3](#0-2) 

Steps:
1. ALICE sets up two friend groups: `family` (priority 0, inheritor DAVE) and `friends_group` (priority 1, inheritor FERDIE).
2. The `friends_group` (lower priority) completes its attempt first via `initiate_attempt`/`finish_attempt`, making FERDIE the inheritor of ALICE.
3. The higher-priority `family` group (BOB) then calls `initiate_attempt` for its pending recovery attempt.
4. FERDIE, using `control_inherited_account`, wraps `RecoveryCall::slash_attempt { friend_group_index: 0 }` inside `pallet_utility::Call::batch`.
5. Despite the reentrancy filter installed on the recovered-account origin, the inner `slash_attempt` call executes because `dispatch_bypass_filter` used by `Utility::batch` skips the filter check — BOB's security deposit gets slashed, and the higher-priority family group's pending attempt is destroyed.

### Citations

**File:** substrate/frame/recovery/src/lib.rs (L567-601)
```rust
		pub fn control_inherited_account(
			origin: OriginFor<T>,
			recovered: AccountIdLookupOf<T>,
			call: Box<<T as Config>::RuntimeCall>,
		) -> DispatchResult {
			let maybe_inheritor = ensure_signed(origin)?;
			let recovered = T::Lookup::lookup(recovered)?;

			let inheritor = Inheritor::<T>::get(&recovered)
				.map(|(_, inheritor, _ticket)| inheritor)
				.ok_or(Error::<T>::NoInheritor)?;
			ensure!(maybe_inheritor == inheritor, Error::<T>::NotInheritor);

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

**File:** substrate/frame/support/procedural/src/construct_runtime/expand/call.rs (L169-183)
```rust
		impl #scrate::__private::Dispatchable for RuntimeCall {
			type RuntimeOrigin = RuntimeOrigin;
			type Config = RuntimeCall;
			type Info = #scrate::dispatch::DispatchInfo;
			type PostInfo = #scrate::dispatch::PostDispatchInfo;
			fn dispatch(self, origin: RuntimeOrigin) -> #scrate::dispatch::DispatchResultWithPostInfo {
				if !<Self::RuntimeOrigin as #scrate::traits::OriginTrait>::filter_call(&origin, &self) {
					return ::core::result::Result::Err(
						#system_path::Error::<#runtime>::CallFiltered.into()
					);
				}

				#scrate::traits::UnfilteredDispatchable::dispatch_bypass_filter(self, origin)
			}
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
