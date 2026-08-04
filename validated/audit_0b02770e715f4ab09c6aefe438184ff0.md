### Title
`process_delivery_receipt` pays relayer reward without checking `DeliveryReceipt.success` - ([File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs])

### Summary
`Pallet::process_delivery_receipt` in the Snowbridge outbound-queue-v2 pallet decodes a `DeliveryReceipt` (which explicitly carries a `success: bool` field taken from the Ethereum `InboundMessageDispatched` event) but never reads or checks that field before paying out the relayer reward and clearing the pending order. This mirrors the H-05 pattern of a settlement path that is not bound to the correct condition/beneficiary state before funds move — here the payout state advances (`PendingOrders` removed, reward registered) even when the on-chain proof itself says the dispatch failed.

### Finding Description
`DeliveryReceipt` is decoded from the Ethereum `InboundMessageDispatched(uint64 nonce, bytes32 topic, bool success, bytes32 reward_address)` event log: [1](#0-0) 

The `success` field is fully populated in the struct, but `process_delivery_receipt` never inspects it. It only checks the gateway address, resolves `reward_account` (defaulting to the caller `relayer` unless a `reward_address` is present), looks up `PendingOrders` by nonce, and unconditionally pays the reward when `order.fee > 0`, then removes the order: [2](#0-1) 

The pallet's own module documentation states the intended flow is: "Fetch the pending order by nonce of the message, pay reward with fee attached in the order... c. Remove the order from `PendingOrders`," with no mention of gating this on the delivery outcome, despite `success` existing purely to signal whether Ethereum-side dispatch of the commands actually succeeded: [3](#0-2) 

The existing "guards" — Merkle/receipt proof verification via `T::Verifier::verify` and the `GatewayAddress` equality check — only establish that the event genuinely came from the configured Gateway contract; they say nothing about whether the dispatched commands succeeded. Because `success` is decoded but discarded, these guards do not stop a relayer from submitting a cryptographically valid receipt for a *failed* dispatch and still being paid in full, and the corresponding `PendingOrder` is irreversibly removed (`<PendingOrders<T>>::remove(nonce)`), permanently closing out the order so it can never be retried, disputed, or repaid correctly.

### Impact Explanation
This breaks the "payout state must only advance after ... execution ... succeed[s]" invariant for bridge settlement. A relayer (an unprivileged, permissionless actor — anyone can call `submit_delivery_receipt`) can be rewarded the full fee for messages whose Ethereum-side execution reverted or otherwise did not complete, i.e., underpriced/unbacked payout not tied to actual delivered work. This is a duplicate/incorrect settlement of value (the `RewardLedger::register_reward` mint/credit) that is unconditionally granted regardless of whether the bridged operation actually completed, directly matching the "duplicate settlement or payout" and "public underpriced work" impact categories for bridge processing.

### Likelihood Explanation
Any relayer submitting a real, verifiable proof from Ethereum can trigger this — no malicious peer, validator, governance, or leaked-key assumption is required. The only precondition is that Ethereum's `InboundMessageDispatched` event can legitimately be emitted with `success = false` (e.g., a dispatched command reverts on execution while the event itself is still emitted), which is exactly the scenario the `success` field was added to distinguish. The test suite for this pallet only exercises the `success: true` path, so there is no existing coverage catching this: [4](#0-3) 

### Recommendation
In `process_delivery_receipt`, gate the reward payment (and the `PendingOrders` removal / `MessageDelivered` event) on `receipt.success`. If `success` is `false`, do not pay the reward and either keep the order pending for the correct terminal handling (e.g., mark it as failed/refundable) or explicitly define a distinct code path that does not disburse relayer funds for a failed dispatch.

### Proof of Concept
1. A message is queued via `do_process_message`, creating `PendingOrders[nonce]` with `fee > 0`.
2. On Ethereum, the Gateway contract attempts to dispatch the message's commands; the dispatch fails, but the Gateway still emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. Any relayer captures this event, builds a valid Merkle/receipt proof, and calls `submit_delivery_receipt`.
4. `T::Verifier::verify` succeeds (the event is genuine), `DeliveryReceipt::try_from` decodes `success = false` but `process_delivery_receipt` never reads it: [5](#0-4) 
5. `T::RewardPayment::register_reward` credits the full `order.fee` to `reward_account`, and `PendingOrders::remove(nonce)` permanently closes the order — the relayer is paid for a message that never successfully executed on Ethereum.

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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L36-42)
```rust
//! 10. When the message has been verified and executed, the relayer will call the extrinsic
//!     `submit_delivery_receipt` to:
//! 	a. Verify the message with proof for a transaction receipt containing the event log,
//! 	   same as the inbound queue verification flow
//! 	b. Fetch the pending order by nonce of the message, pay reward with fee attached in the order
//!    	c. Remove the order from `PendingOrders` map storage by nonce
//!
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

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound.rs (L398-427)
```rust
	BridgeHubWestend::execute_with(|| {
		type RuntimeEvent = <BridgeHubWestend as Chain>::RuntimeEvent;

		// Check that the Ethereum message was queue in the Outbound Queue
		assert_expected_events!(
			BridgeHubWestend,
			vec![RuntimeEvent::EthereumOutboundQueueV2(snowbridge_pallet_outbound_queue_v2::Event::MessageQueued{ .. }) => {},]
		);

		let relayer = BridgeHubWestendSender::get();
		let reward_account = AssetHubWestendReceiver::get();
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
}
```
