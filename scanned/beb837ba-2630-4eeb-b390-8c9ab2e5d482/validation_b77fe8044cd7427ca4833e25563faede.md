### Title
`process_delivery_receipt` in Snowbridge `outbound-queue-v2` never checks the decoded `success` flag before paying the relayer reward - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
`DeliveryReceipt` decoded from the Ethereum `InboundMessageDispatched` event carries a `success: bool` field indicating whether the message actually executed successfully on the Gateway contract. `Pallet::process_delivery_receipt` verifies the gateway address and the pending nonce, but never inspects `receipt.success` before paying out the relayer reward and clearing the `PendingOrder`. This is structurally the same class of bug as the reported issue: a field that is decoded and intended to gate an action (there, source-address validation; here, execution-success validation) is silently ignored, so the guard the protocol relies on never actually executes.

### Finding Description
The `DeliveryReceipt` type is decoded directly from the `InboundMessageDispatched(uint64 indexed nonce, bytes32 topic, bool success, bytes32 reward_address)` Ethereum event: [1](#0-0) 

The pallet's `process_delivery_receipt` function is the settlement point that pays relayer rewards and finalizes the corresponding `PendingOrder`: [2](#0-1) 

The logic only checks:
1. `receipt.gateway` matches `T::GatewayAddress::get()`.
2. `PendingOrders::<T>::get(nonce)` exists.

It then unconditionally pays `order.fee` to the reward account and removes the `PendingOrder`, regardless of the value of `receipt.success`. The `success` field is decoded into the `DeliveryReceipt` struct but is dropped on the floor — there is no `ensure!(receipt.success, ...)` or any branching on it anywhere in `process_delivery_receipt`.

### Impact Explanation
`PendingOrders` and the associated relayer-reward payout are meant to represent "the message was delivered and dispatched on Ethereum" as verified by an on-chain proof of the Ethereum receipt event. Because `success` is never checked:
- A relayer can submit a valid proof for an `InboundMessageDispatched` event where `success = false` (i.e., the Gateway contract reverted/failed to execute the command on Ethereum) and still receive the full relayer reward as if delivery succeeded.
- The `PendingOrder` is removed either way, permanently closing out the settlement state for that nonce — there is no retry/compensation path visible in this pallet for a failed dispatch, since the order bookkeeping (`PendingOrders`) is the only state tracking that nonce's lifecycle on this side.
- This breaks the "settlement state must only advance after execution succeeds" invariant: reward payout and order finalization proceed even when the cross-chain command execution demonstrably failed.

This does not require a malicious relayer to forge anything — it only requires that a legitimately relayed message failed to execute on the Ethereum side for any reason (revert, gas issue, contract-level rejection), which is a normal, expected occurrence in cross-chain messaging, not an attack precondition. Any relayer (including an honest one) submitting a genuine receipt proof for a failed dispatch triggers unconditional payout, i.e., public underpriced/incorrect settlement.

### Likelihood Explanation
High. This code path is reachable by any signed account via the public extrinsic `submit_delivery_receipt`, which only requires `ensure_signed` and a valid Merkle/receipt proof verified by `T::Verifier`: [3](#0-2) 

No governance, admin, or malicious-relayer assumption is needed — a normally operating relayer that simply relays a receipt for a message whose execution failed on Ethereum (which will happen in practice) triggers the incorrect unconditional payout and order removal.

### Recommendation
In `process_delivery_receipt`, add an explicit check on `receipt.success` before paying the reward and/or before unconditionally removing the `PendingOrder`:
```rust
ensure!(receipt.success, Error::<T>::MessageDispatchFailed);
```
or, if the intended design is to reward relayers purely for delivery effort independent of execution outcome, that design choice should be explicit in code and documented, and a separate signal/event should be used to track failed dispatches (e.g., different event, no silent removal, or a distinct handling path) so failed executions are distinguishable and auditable rather than being treated identically to successful ones.

### Proof of Concept
1. A message is queued in the outbound queue and a `PendingOrder { nonce, fee, .. }` is inserted (`Messages::<T>::append` / `<PendingOrders<T>>::insert(nonce, order)` in `do_process_message`).
2. On Ethereum, delivery of this message to the Gateway contract fails (e.g., reverts during `InboundMessageDispatched` emission with `success = false`), which is a normal occurrence, not an attack.
3. A relayer builds a valid Merkle/receipt proof for the actual `InboundMessageDispatched(nonce, topic, success=false, reward_address)` event and calls `submit_delivery_receipt(origin, event)`.
4. `T::Verifier::verify` succeeds (the proof is genuine), `DeliveryReceipt::try_from` decodes `success: false` correctly.
5. `process_delivery_receipt` is invoked with this receipt: gateway matches, `PendingOrders::get(nonce)` exists, `order.fee > 0` → `T::RewardPayment::register_reward` pays the relayer/reward account the full fee, and `PendingOrders::remove(nonce)` finalizes the order — despite `receipt.success == false`.

This can be confirmed by inspecting the existing test suite in `bridges/snowbridge/pallets/outbound-queue-v2/src/test.rs`, none of which appears to construct a receipt with `success: false` and assert that payout is withheld — all example receipts (e.g. `snowbridge_v2_outbound.rs` fixtures) simply set `success: true` and check the reward is paid, without any counter-test that a `success: false` receipt is rejected. [4](#0-3)

### Citations

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/delivery_receipt.rs (L10-27)
```rust
sol! {
	event InboundMessageDispatched(uint64 indexed nonce, bytes32 topic, bool success, bytes32 reward_address);
}

/// Delivery receipt
#[derive(Clone, Debug)]
pub struct DeliveryReceipt {
	/// The address of the outbound queue on Ethereum that emitted this message as an event log
	pub gateway: H160,
	/// The nonce of the dispatched message
	pub nonce: u64,
	/// Message topic
	pub topic: H256,
	/// Delivery status
	pub success: bool,
	/// The reward address
	pub reward_address: [u8; 32],
}
```

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

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound.rs (L105-122)
```rust
		let receipt = DeliveryReceipt {
			gateway: EthereumGatewayAddress::get(),
			nonce: 1,
			reward_address: reward_account.into(),
			topic: H256::zero(),
			success: true,
		};

		// Submit a delivery receipt
		assert_ok!(EthereumOutboundQueueV2::process_delivery_receipt(relayer, receipt));

		assert_expected_events!(
			BridgeHubWestend,
			vec![
				RuntimeEvent::BridgeRelayers(pallet_bridge_relayers::Event::RewardRegistered { .. }) => {},
			]
		);
	});
```
