This confirms the claim exactly as stated. `process_delivery_receipt` in `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs` decodes `DeliveryReceipt` with a `success` field but never reads it before paying out the reward and removing the pending order.No further searches needed — the grep confirms `success` is never referenced anywhere in the pallet, and `register_reward` is called unconditionally alongside `PendingOrders::remove(nonce)`. The claim is fully substantiated by the code.

Audit Report

## Title
`process_delivery_receipt` pays relayer reward without checking `DeliveryReceipt.success` - ([File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs])

## Summary
`Pallet::process_delivery_receipt` decodes a `DeliveryReceipt` containing a `success: bool` field sourced from the Ethereum `InboundMessageDispatched` event, but never inspects that field before crediting the relayer reward via `T::RewardPayment::register_reward` and permanently removing the `PendingOrders` entry. A relayer can submit a cryptographically valid receipt for a message whose Ethereum-side dispatch failed (`success = false`) and still be paid the full fee, with the order irreversibly closed.

## Finding Description
`DeliveryReceipt` is decoded from the `InboundMessageDispatched(uint64 nonce, bytes32 topic, bool success, bytes32 reward_address)` event log, fully populating the `success` field: [1](#0-0) 

In `Pallet::process_delivery_receipt`, only `receipt.gateway`, `receipt.reward_address`, and `receipt.nonce` are used; `receipt.success` is decoded but never read anywhere in the pallet (confirmed via search — no occurrence of `success` in the pallet source). The function unconditionally pays the reward when `order.fee > 0` and then removes the order: [2](#0-1) 

The module doc describes the intended flow as verify → fetch pending order → pay reward → remove order, with no gating on delivery outcome despite `success` existing specifically to signal Ethereum-side dispatch outcome: [3](#0-2) 

The only guards present — `T::Verifier::verify` (Merkle/receipt proof authenticity) and the `GatewayAddress` equality check — establish that the event genuinely originated from the configured Gateway contract, but they say nothing about whether the dispatched commands succeeded on Ethereum. `submit_delivery_receipt` is a permissionless, signed extrinsic reachable by any relayer: [4](#0-3) 

## Impact Explanation
This breaks the invariant that bridge payout/settlement state must only advance after execution actually succeeds. Because `PendingOrders::remove(nonce)` is unconditional and irreversible, a message whose Ethereum dispatch failed is permanently closed out and its relayer is paid in full regardless of outcome — an unbacked/duplicate settlement of the bridge reward ledger (`T::RewardPayment::register_reward`) that directly matches the "duplicate settlement or payout" / "public underpriced work that ... stalls bridge processing" impact categories for the Snowbridge outbound-queue-v2 pallet.

## Likelihood Explanation
Any relayer holding a legitimate proof for a real `InboundMessageDispatched` event can trigger this without needing malicious peer, validator, governance, or key-compromise assumptions — the only precondition is that the Gateway contract on Ethereum can legitimately emit this event with `success = false` (i.e., a dispatched command reverts while the event is still emitted), which is exactly the condition the field exists to represent. The pallet's own test coverage only exercises the `success: true` path, confirming this gap is untested: [5](#0-4) 

## Recommendation
In `process_delivery_receipt`, branch on `receipt.success`: only call `T::RewardPayment::register_reward` and emit `MessageDelivered` (and remove the order) when `success` is `true`. When `success` is `false`, avoid paying the reward and define an explicit terminal/failure path for the order (e.g., a distinct event/state, or retry/refund handling) instead of silently closing it out as if delivery succeeded.

## Proof of Concept
1. A message is queued via `do_process_message`, creating `PendingOrders[nonce]` with `fee > 0`: [6](#0-5) 
2. On Ethereum, the Gateway contract's dispatch of the message's commands fails but still emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. A relayer builds a valid Merkle/receipt proof for this genuine event and calls `submit_delivery_receipt`.
4. `T::Verifier::verify` succeeds and `DeliveryReceipt::try_from` decodes `success = false`, but `process_delivery_receipt` never reads it: [7](#0-6) 
5. `register_reward` credits `order.fee` to `reward_account` and `PendingOrders::remove(nonce)` permanently closes the order, i.e. the relayer is paid despite the failed dispatch, and the order can never be retried or repaid correctly.

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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L36-41)
```rust
//! 10. When the message has been verified and executed, the relayer will call the extrinsic
//!     `submit_delivery_receipt` to:
//! 	a. Verify the message with proof for a transaction receipt containing the event log,
//! 	   same as the inbound queue verification flow
//! 	b. Fetch the pending order by nonce of the message, pay reward with fee attached in the order
//!    	c. Remove the order from `PendingOrders` map storage by nonce
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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L426-436)
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
