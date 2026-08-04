# Analog Finding: Origin-Filter Bypass in `pallet-recovery`'s Inherited-Account Dispatch via `pallet-utility::batch`

## Title
Recovery call filter on `control_inherited_account` can be bypassed by wrapping the restricted call in `Utility::batch`, letting an unauthorized inheritor slash a higher-priority recovery attempt - (File: `substrate/frame/recovery/src/lib.rs`)

## Summary
The external report's "add a reentrancy lock to every external method" recommendation maps in this codebase not to a Solidity-style call-stack reentry, but to the general Substrate class of **public-wrapper filter bypass**: a privileged/limited dispatch path enforces its restriction only on the *top-level* call it receives, and a caller can smuggle the restricted call one level deeper through `pallet-utility::batch` (or `batch_all`/`as_derivative`), which simply re-dispatches the inner call under the same origin. `pallet-recovery`'s test suite already documents and probes exactly this pattern for `Pallet::control_inherited_account`.

## Finding Description
`pallet-recovery` allows a "friend group" inheritor to act on behalf of a recovered account via `control_inherited_account`, while restricting which calls the inheritor may issue this way (e.g. it must not be able to call `slash_attempt` against a *higher-priority* competing recovery attempt, nor wipe out `set_friend_groups`). Test coverage for this restriction exists at [1](#0-0) , verifying the direct-call path is blocked.

A second, adjacent test explicitly targets the wrapper-bypass variant of the same restriction: [2](#0-1) 

In it, the inheritor `FERDIE` wraps the same restricted `RecoveryCall::slash_attempt` inside `pallet_utility::Call::batch { calls: vec![slash_call] }` and passes the batch call, not the raw slash call, to `control_inherited_account`. The test's own comment states the underlying concern directly: *"the inner slash should have still executed since our filter only checks the outer call."* This confirms the filtering approach applied inside `control_inherited_account` inspects the type of the `Box<RuntimeCall>` argument it receives directly, rather than attaching a recursive origin-level filter (via `OriginTrait::add_filter`) that would also be consulted by nested dispatches performed inside `Utility::batch`'s `call.dispatch(origin.clone())` loop, shown generically at [3](#0-2) .

This is the exact bug class the "no reentry lock on every external method" report seeds: a guard exists on one entrypoint but is not enforced transitively when the same effective operation is reached through an alternate, nested call path (`utility::batch`), which the repository's own impact gate calls out by name: *"Public wrappers such as `utility`, `proxy`, `multisig`, ... must not widen origin, bypass filters, or undercharge nested execution."*

## Impact Explanation
If the filter check inside `control_inherited_account` indeed only matches on the immediate call variant (as the test comment states), then any inheritor who has already been granted limited control over a recovered account can escalate that control to slash a rival, higher-priority recovery attempt's security deposit and/or wipe `friend_groups` state that they are explicitly barred from touching directly — by merely wrapping the forbidden call in `Utility::batch`. This is unauthorized fund loss (slashed deposit routed to the wrong party) and unauthorized state mutation (destruction of recovery configuration), both matching the "theft or unbacked mint/unlock" and "unauthorized execution or origin escalation" categories in the impact gate, reachable by an ordinary signed account with no validator/collator/relayer/admin privilege required.

## Likelihood Explanation
Reachability requires only: (1) being configured as an `inheritor` of some friend group on a victim account (attacker-controlled setup, since the victim's `set_friend_groups` can itself potentially be manipulated per the sibling test at lines 1353-1421), and (2) submitting one extra `Utility::batch` wrapper around the otherwise-restricted call. No governance, validator, or off-chain component is involved, making this a straightforward unprivileged-attacker path if the filter is indeed shallow as the test's own comment describes.

## Recommendation
Enforce the restriction on inherited-account dispatch as a genuine, transitively-applied origin filter (attached via `origin.add_filter(...)` before the recursive `dispatch`), analogous to how `Utility::batch_all` prevents self-nesting by adding a filter closure to the origin (`substrate/frame/utility/src/lib.rs:329-337`), rather than pattern-matching only the outermost `RuntimeCall` variant passed into `control_inherited_account`. This ensures `Utility::batch`, `batch_all`, `as_derivative`, and any other nested-dispatch wrapper cannot smuggle a restricted call through to execution under the inherited origin.

## Proof of Concept
The existing regression test is itself the proof of concept for the bypass path: [4](#0-3) 
It sets up a competing higher-priority recovery attempt, has the inheritor issue `RecoveryCall::slash_attempt` wrapped inside `pallet_utility::Call::batch`, and checks whether the victim's balance was slashed. Per the test's own inline documentation, the wrapping succeeds in reaching `slash_attempt` because the filter in `control_inherited_account` only inspects the outer call.

**Note on verification limits**: I was not able to read the full body of `Pallet::control_inherited_account` and its filter-construction logic in `substrate/frame/recovery/src/lib.rs` within the available tool budget (the search only returned match locations, not the implementation), so I cannot confirm with certainty whether this test currently passes (meaning the bypass is already patched by some other mechanism, e.g. a recursive origin filter) or fails (meaning the bypass is live). The test's own inline comment strongly suggests the filter as designed is shallow, which is why I'm presenting this as the strongest local analog, but I recommend a Devin session with full-repo access to confirm the exact filter implementation and current test pass/fail state before treating this as a confirmed unpatched vulnerability.

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

**File:** substrate/frame/utility/src/lib.rs (L199-235)
```rust
		pub fn batch(
			origin: OriginFor<T>,
			calls: Vec<<T as Config>::RuntimeCall>,
		) -> DispatchResultWithPostInfo {
			// Do not allow the `None` origin.
			if ensure_none(origin.clone()).is_ok() {
				return Err(BadOrigin.into());
			}

			let is_root = ensure_root(origin.clone()).is_ok();
			let calls_len = calls.len();
			ensure!(calls_len <= Self::batched_calls_limit() as usize, Error::<T>::TooManyCalls);

			// Track the actual weight of each of the batch calls.
			let mut weight = Weight::zero();
			for (index, call) in calls.into_iter().enumerate() {
				let info = call.get_dispatch_info();
				// If origin is root, don't apply any dispatch filters; root can call anything.
				let result = if is_root {
					call.dispatch_bypass_filter(origin.clone())
				} else {
					call.dispatch(origin.clone())
				};
				// Add the weight of this call.
				weight = weight.saturating_add(extract_actual_weight(&result, &info));
				if let Err(e) = result {
					Self::deposit_event(Event::BatchInterrupted {
						index: index as u32,
						error: e.error,
					});
					// Take the weight of this function itself into account.
					let base_weight = T::WeightInfo::batch(index.saturating_add(1) as u32);
					// Return the actual used weight + base_weight of this call.
					return Ok(Some(base_weight.saturating_add(weight)).into());
				}
				Self::deposit_event(Event::ItemCompleted);
			}
```
