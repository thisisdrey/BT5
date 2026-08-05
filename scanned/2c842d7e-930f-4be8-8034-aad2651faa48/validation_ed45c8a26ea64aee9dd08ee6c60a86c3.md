Based on my investigation, I found a solid local analog in the Snowbridge outbound-queue-v2 pallet's delivery-receipt handling: the `success` field of a verified Ethereum delivery receipt is decoded but never checked before paying the relayer and clearing the pending order — mirroring the external bug's core flaw (a field that should gate a payout/acceptance decision is present in the data structure but not actually enforced, so the code proceeds as if the condition were satisfied).

### Title
Relayer reward is paid and `PendingOrder` cleared regardless of `DeliveryReceipt.success` - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
`Pallet::process_delivery_receipt` verifies the Ethereum event proof and decodes it into a `DeliveryReceipt`, which contains a `success: bool` field describing whether the corresponding message actually executed successfully on the Ethereum Gateway contract. The pallet's payout logic never reads or checks this field — it only checks the gateway address, looks up the `PendingOrder` by nonce, and pays the fee if `order.fee > 0`, then unconditionally removes the order.

### Finding Description
`process_delivery_receipt` in [1](#0-0)  performs:
1. `ensure!(T::GatewayAddress::get() == receipt.gateway, ...)`
2. Resolve `reward_account` (falls back to `relayer` if `reward_address == [0u8;32]`)
3. `let order = <PendingOrders<T>>::get(nonce).ok_or(Error::<T>::InvalidPendingNonce)?;`
4. `if order.fee > 0 { T::RewardPayment::register_reward(...) }`
5. `<PendingOrders<T>>::remove(nonce);`

At no point is `receipt.success` inspected. The `DeliveryReceipt` struct explicitly carries a `success` field [2](#0-1) , and the integration test suite constructs receipts with `success: true` [3](#0-2) , showing the field is expected to carry semantic meaning about execution outcome — yet the runtime pallet ignores it entirely.

This is structurally the same class of bug as the QuadrataKYCVerifier issue: a data field meant to gate a critical decision (AML-attribute presence/expiry vs. delivery success) is either missing (epoch==0) or simply not consulted (`success` unused), so the guarded action (investment / relayer payout + order settlement) proceeds as though the condition had been satisfied.

### Impact Explanation
Any relayer can submit a validly-proved Ethereum event log for a Gateway-emitted delivery event whose `success` flag is `false` (i.e., the corresponding cross-chain command failed/reverted on Ethereum) and still: (a) receive the full relayer fee reward via `T::RewardPayment::register_reward`, and (b) have the `PendingOrder` removed from `PendingOrders`, permanently closing out tracking for that nonce. This causes an unbacked/duplicate-style payout — the relayer is rewarded for work that did not actually complete — and the pending-order bookkeeping is retired even though the underlying message delivery failed, which can mask failed message states and break any downstream retry/monitoring logic that depends on `PendingOrders` reflecting outstanding, unresolved deliveries.

### Likelihood Explanation
The `submit_delivery_receipt` extrinsic is a public, unprivileged entry point requiring only a valid Ethereum event-log verification (`T::Verifier::verify`) and a correctly ABI-decoded `DeliveryReceipt`; no special origin or governance is needed. Because Ethereum-side execution failures (reverts, out-of-gas commands, etc.) are a normal, non-adversarial occurrence, a relayer does not even need to act maliciously — it can simply relay a legitimate but failed delivery receipt and still collect payment, making this reachable in ordinary operation, not just via attacker-crafted inputs.

### Recommendation
Check `receipt.success` in `process_delivery_receipt` before paying the reward and removing the order: only pay the relayer and clear the `PendingOrder` when `receipt.success` is `true`; on `false`, either leave the order pending (so any retry/refund/cleanup logic can act on it) or transition the order to a distinct "failed" state, and avoid crediting `T::RewardPayment` for a failed execution.

### Proof of Concept
1. A message is queued through `do_process_message`, creating `PendingOrders::<T>::insert(nonce, PendingOrder { nonce, fee, block_number })` with `fee > 0` [4](#0-3) .
2. On Ethereum, the corresponding command execution fails (reverts), and the Gateway contract emits a delivery event with `success = false`.
3. A relayer (any signed account) submits this event log with a valid Merkle/verifier proof via `submit_delivery_receipt` [5](#0-4) .
4. `T::Verifier::verify` succeeds (the log is authentic), `DeliveryReceipt::try_from` decodes `success: false` correctly.
5. `process_delivery_receipt` proceeds: gateway matches, `PendingOrders::get(nonce)` returns `Some(order)`, `order.fee > 0` is true, so `T::RewardPayment::register_reward` pays the relayer the full fee, and `PendingOrders::remove(nonce)` deletes the order — despite the underlying delivery having failed on Ethereum. The existing test `submit_delivery_receipt_succeeds_after_unhalt` [6](#0-5)  demonstrates this exact pay-and-remove path using `mock_valid_event_proof()`, which never varies or asserts on the `success` field.

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L298-317)
```rust
		#[pallet::call_index(1)]
		#[pallet::weight(T::WeightInfo::submit_delivery_receipt())]
		pub fn submit_delivery_receipt(
			origin: OriginFor<T>,
			event: Box<EventProof>,
		) -> DispatchResult
		where
			<T as frame_system::Config>::AccountId: From<[u8; 32]>,
		{
			let relayer = ensure_signed(origin)?;

			// submit message to verifier for verification
			T::Verifier::verify(&event.event_log, &event.proof)
				.map_err(|e| Error::<T>::Verification(e))?;

			let receipt = DeliveryReceipt::try_from(&event.event_log)
				.map_err(|_| Error::<T>::InvalidEnvelope)?;

			Self::process_delivery_receipt(relayer, receipt)
		}
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L426-438)
```rust
			// Generate `PendingOrder` with fee attached in the message, stored
			// into the `PendingOrders` map storage, with assigned nonce as the key.
			// When the message is processed on ethereum side, the relayer will send the nonce
			// back with delivery proof, only after that the order can
			// be resolved and the fee will be rewarded to the relayer.
			let order = PendingOrder {
				nonce,
				fee,
				block_number: frame_system::Pallet::<T>::current_block_number(),
			};
			<PendingOrders<T>>::insert(nonce, order);

			<Nonce<T>>::set(nonce);
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L446-480)
```rust
		pub fn process_delivery_receipt(
			relayer: <T as frame_system::Config>::AccountId,
			receipt: DeliveryReceipt,
		) -> DispatchResult
		where
			<T as frame_system::Config>::AccountId: From<[u8; 32]>,
		{
			// Verify that the message was submitted from the known Gateway contract
			ensure!(T::GatewayAddress::get() == receipt.gateway, Error::<T>::InvalidGateway);

			let reward_account = if receipt.reward_address == [0u8; 32] {
				relayer
			} else {
				receipt.reward_address.into()
			};

			let nonce = receipt.nonce;

			let order = <PendingOrders<T>>::get(nonce).ok_or(Error::<T>::InvalidPendingNonce)?;

			if order.fee > 0 {
				// Pay relayer reward
				T::RewardPayment::register_reward(
					&reward_account,
					T::DefaultRewardKind::get(),
					order.fee,
				);
			}

			<PendingOrders<T>>::remove(nonce);

			Self::deposit_event(Event::MessageDelivered { nonce });

			Ok(())
		}
```

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/delivery_receipt.rs (L1-1)
```rust
// SPDX-License-Identifier: Apache-2.0
```

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound.rs (L949-968)
```rust
#[test]
fn invalid_nonce_for_delivery_receipt_fails() {
	BridgeHubWestend::execute_with(|| {
		type Runtime = <BridgeHubWestend as Chain>::Runtime;

		let relayer = BridgeHubWestendSender::get();
		let reward_account = AssetHubWestendReceiver::get();
		let receipt = DeliveryReceipt {
			gateway: EthereumGatewayAddress::get(),
			nonce: 0,
			reward_address: reward_account.into(),
			topic: H256::zero(),
			success: true,
		};

		assert_err!(
			EthereumOutboundQueueV2::process_delivery_receipt(relayer, receipt),
			Error::<Runtime>::InvalidPendingNonce
		);
	});
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/test.rs (L421-449)
```rust
fn submit_delivery_receipt_succeeds_after_unhalt() {
	new_tester().execute_with(|| {
		let nonce = 0;
		let fee: u128 = 1_000_000;
		let order = PendingOrder { nonce, fee, block_number: System::block_number() };
		PendingOrders::<Test>::insert(nonce, order);

		let relayer: AccountId32 = [7u8; 32].into();
		let origin = RuntimeOrigin::signed(relayer);
		let event = Box::new(mock_valid_event_proof());

		// Bridge halted — receipt rejected, order untouched.
		set_verifier_halted(true);
		assert_noop!(
			OutboundQueue::submit_delivery_receipt(origin.clone(), event.clone()),
			Error::<Test>::Verification(VerificationError::Halted)
		);
		assert!(PendingOrders::<Test>::get(nonce).is_some());

		// Bridge resumed — same receipt succeeds and the order is settled.
		set_verifier_halted(false);
		assert_ok!(OutboundQueue::submit_delivery_receipt(origin, event));
		assert!(PendingOrders::<Test>::get(nonce).is_none());

		System::assert_has_event(mock::RuntimeEvent::OutboundQueue(Event::MessageDelivered {
			nonce,
		}));
	});
}
```
