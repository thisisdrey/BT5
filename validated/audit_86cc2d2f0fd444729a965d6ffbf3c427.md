### Title
Single `Halted` operating-mode flag blocks message reception and delivery-confirmation together, forcing bridge owners to reopen the vulnerable inbound path in order to unblock outbound settlement - ([File: bridges/modules/messages/src/lib.rs])

### Summary
The reported `sDaiStrategy` bug boils down to one broken invariant: a single boolean gate controls two opposite flows (deposit vs. withdrawal), so recovering the withdrawal path forces the deposit path back open too. The same invariant is broken in `pallet-bridge-messages` (and every other `OwnedBridgeModule` implementer in `bridges/`): a single `PalletOperatingMode`/`BasicOperatingMode::Halted` flag gates **both** the inbound message-acceptance entrypoint (`receive_messages_proof`, the "deposit" side) and the delivery-confirmation/settlement entrypoint (`receive_messages_delivery_proof`, the "withdrawal"/payout side). There is no way to halt only new message intake while still letting relayers finalize already-delivered messages and collect rewards.

### Finding Description
`OwnedBridgeModule::is_halted()`/`ensure_not_halted()` reads a single `OperatingModeStorage` value and returns one binary result used to gate every dispatchable of the pallet: [1](#0-0) 

`pallet-bridge-messages` uses exactly this mechanism for both of its externally-callable proof-processing entrypoints. The test `pallet_rejects_transactions_if_halted` demonstrates that setting `PalletOperatingMode::Halted` simultaneously rejects `receive_messages_proof` (accepting new inbound messages) **and** `receive_messages_delivery_proof` (confirming delivery back to the source chain, which triggers relayer reward accounting via `OnMessagesDelivered`): [2](#0-1) 

The same coupled-flag pattern is repeated verbatim in `pallet-bridge-parachains` and `pallet-bridge-beefy`, where a single `PalletOperatingMode` storage value halts/resumes "all pallet operations": [3](#0-2) [4](#0-3) 

This mirrors the `sDaiStrategy.emergencyWithdraw()` flaw precisely: if the pallet owner discovers a problem in the inbound flow (e.g. a bad proof being accepted, or a stuck/malicious relayer submission) and halts the pallet as an emergency measure, they cannot separately re-enable only `receive_messages_delivery_proof` to let honest relayers finalize deliveries and claim their pending rewards. To unblock the withdrawal/settlement side, the owner (or `Root`/governance via `set_operating_mode`) must set the mode back to `Normal`, which immediately reopens `receive_messages_proof` as well — restoring exactly the exploitable inbound condition that motivated the halt in the first place. An unprivileged relayer/attacker can then immediately resubmit proofs through the reopened inbound path before a fix or a second halt can be applied, in the same way that an attacker in the original report could re-trigger `sDaiStrategy`'s auto-deposit by simply calling `deposit()` right after the strategy was unpaused.

### Impact Explanation
This breaks the "message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically" invariant at the operational-control level: the settlement/payout path (delivery confirmation, relayer reward accrual) is architecturally inseparable from the message-intake path. Either:
- Relayer rewards and delivery confirmations remain **permanently stuck** while the pallet stays halted (fund/state lock impacting relayer payouts and lane state), or
- The owner is forced to reopen the very inbound channel that necessitated the halt, letting an unprivileged party resume submitting messages/proofs through the reopened channel, potentially causing further message flow to be processed under the same unresolved condition (stalled/degraded bridge processing, or continued acceptance of the problematic inbound state).

### Likelihood Explanation
Medium: halting a bridge module is an intentional emergency admin action, not a routine one, matching the "medium likelihood" rating of the source report. But once triggered, the coupling is unconditional and deterministic — every `Halted`/`Normal` toggle in this trait affects all gated calls identically, so the flaw is guaranteed to manifest whenever a partial recovery (settle-only, no-new-intake) is needed.

### Recommendation
Split `BasicOperatingMode`/`OwnedBridgeModule` into independently toggleable modes for inbound message acceptance versus delivery-confirmation/settlement processing (mirroring the already-existing `MessagesOperatingMode::RejectingOutboundMessages` idea, but applied symmetrically to the inbound-vs-confirmation axis), so an owner can halt new message intake while still allowing `receive_messages_delivery_proof` (and associated reward settlement) to proceed to completion. At minimum, `ensure_not_halted()` should be parameterized per call-category rather than gating the whole pallet with one flag.

### Proof of Concept
1. Relayer submits messages via `receive_messages_proof`; nonce N is accepted into the inbound lane and awaiting dispatch/reward settlement.
2. Pallet owner detects a problem with inbound proof acceptance (e.g., a bug allowing malformed/duplicate proofs) and calls `set_operating_mode(Halted)` via `OwnedBridgeModule::set_operating_mode`.
3. Honest relayers who already delivered messages can no longer call `receive_messages_delivery_proof` to confirm delivery and claim rewards — see `bridges/modules/messages/src/tests/pallet_tests.rs:119-167`, both calls fail with `BridgeModule(Halted)`.
4. To unblock relayer settlement, the owner must call `set_operating_mode(Normal)`, which simultaneously reopens `receive_messages_proof`.
5. An unprivileged actor immediately resubmits proofs through the reopened inbound path, reproducing the original condition that triggered the halt — analogous to `sDaiStrategy` re-depositing into the pool as soon as `paused` is cleared.

### Citations

**File:** bridges/primitives/runtime/src/lib.rs (L389-413)
```rust
	/// Check if the module is halted.
	fn is_halted() -> bool {
		Self::OperatingModeStorage::get().is_halted()
	}

	/// Ensure that the origin is either root, or `PalletOwner`.
	fn ensure_owner_or_root(origin: T::RuntimeOrigin) -> Result<(), BadOrigin> {
		match origin.into() {
			Ok(RawOrigin::Root) => Ok(()),
			Ok(RawOrigin::Signed(ref signer))
				if Self::OwnerStorage::get().as_ref() == Some(signer) =>
			{
				Ok(())
			},
			_ => Err(BadOrigin),
		}
	}

	/// Ensure that the module is not halted.
	fn ensure_not_halted() -> Result<(), OwnedBridgeModuleError> {
		match Self::is_halted() {
			true => Err(OwnedBridgeModuleError::Halted),
			false => Ok(()),
		}
	}
```

**File:** bridges/modules/messages/src/tests/pallet_tests.rs (L119-167)
```rust
#[test]
fn pallet_rejects_transactions_if_halted() {
	run_test(|| {
		// send message first to be able to check that delivery_proof fails later
		send_regular_message(test_lane_id());

		PalletOperatingMode::<TestRuntime, ()>::put(MessagesOperatingMode::Basic(
			BasicOperatingMode::Halted,
		));

		assert_noop!(
			Pallet::<TestRuntime, ()>::validate_message(test_lane_id(), &REGULAR_PAYLOAD),
			Error::<TestRuntime, ()>::NotOperatingNormally,
		);

		let messages_proof = prepare_messages_proof(vec![message(2, REGULAR_PAYLOAD)], None);
		assert_noop!(
			Pallet::<TestRuntime>::receive_messages_proof(
				RuntimeOrigin::signed(1),
				TEST_RELAYER_A,
				messages_proof,
				1,
				REGULAR_PAYLOAD.declared_weight,
			),
			Error::<TestRuntime, ()>::BridgeModule(bp_runtime::OwnedBridgeModuleError::Halted),
		);

		let delivery_proof = prepare_messages_delivery_proof(
			test_lane_id(),
			InboundLaneData {
				state: LaneState::Opened,
				last_confirmed_nonce: 1,
				relayers: vec![unrewarded_relayer(1, 1, TEST_RELAYER_A)].into(),
			},
		);
		assert_noop!(
			Pallet::<TestRuntime>::receive_messages_delivery_proof(
				RuntimeOrigin::signed(1),
				delivery_proof,
				UnrewardedRelayersState {
					unrewarded_relayer_entries: 1,
					messages_in_oldest_entry: 1,
					total_messages: 1,
					last_delivered_nonce: 1,
				},
			),
			Error::<TestRuntime, ()>::BridgeModule(bp_runtime::OwnedBridgeModuleError::Halted),
		);
		assert_ok!(Pallet::<TestRuntime>::do_try_state());
```

**File:** bridges/modules/parachains/src/lib.rs (L260-275)
```rust
	/// Optional pallet owner.
	///
	/// Pallet owner has a right to halt all pallet operations and then resume them. If it is
	/// `None`, then there are no direct ways to halt/resume pallet operations, but other
	/// runtime methods may still be used to do that (i.e. democracy::referendum to update halt
	/// flag directly or call the `set_operating_mode`).
	#[pallet::storage]
	pub type PalletOwner<T: Config<I>, I: 'static = ()> =
		StorageValue<_, T::AccountId, OptionQuery>;

	/// The current operating mode of the pallet.
	///
	/// Depending on the mode either all, or no transactions will be allowed.
	#[pallet::storage]
	pub type PalletOperatingMode<T: Config<I>, I: 'static = ()> =
		StorageValue<_, BasicOperatingMode, ValueQuery>;
```

**File:** bridges/modules/beefy/src/lib.rs (L311-326)
```rust
	/// Optional pallet owner.
	///
	/// Pallet owner has the right to halt all pallet operations and then resume it. If it is
	/// `None`, then there are no direct ways to halt/resume pallet operations, but other
	/// runtime methods may still be used to do that (i.e. `democracy::referendum` to update halt
	/// flag directly or calling `set_operating_mode`).
	#[pallet::storage]
	pub type PalletOwner<T: Config<I>, I: 'static = ()> =
		StorageValue<_, T::AccountId, OptionQuery>;

	/// The current operating mode of the pallet.
	///
	/// Depending on the mode either all, or no transactions will be allowed.
	#[pallet::storage]
	pub type PalletOperatingMode<T: Config<I>, I: 'static = ()> =
		StorageValue<_, BasicOperatingMode, ValueQuery>;
```
