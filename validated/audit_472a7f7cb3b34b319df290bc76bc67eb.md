This confirms the claim is invalid. The `DeliveryReceipt` struct decoded from the Ethereum `InboundMessageDispatched` event explicitly includes a `success: bool` field [1](#0-0) , and this field is populated correctly during decode via `TryFrom<&Log>` [2](#0-1) . The report's own caveat noted that if the Gateway event includes a success field that isn't silently dropped, the analog does not hold — and that is exactly what's confirmed here: the `success` field exists on `DeliveryReceipt` and is decoded from the Solidity event's `bool success` parameter.

However, `process_delivery_receipt` in `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs` (lines 446-480) never reads `receipt.success` before paying the reward and removing the `PendingOrders` entry — it only checks `receipt.gateway` and `order.fee > 0`. This is corroborated by the integration test `invalid_nonce_for_delivery_receipt_fails` and others in `cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound.rs`, all of which construct `DeliveryReceipt { success: true, ... }` and none of which test a `success: false` case being rejected or handled differently [3](#0-2) .

So there genuinely is no branch on `receipt.success` in the settlement path — the field is decoded but discarded. This matches the reported bug pattern precisely: a boolean success indicator is decoded but never consulted before settlement/payout. This is a real, confirmed gap, not a speculative analog.

Given the field is present and decoded but is dropped/unused in the actual settlement logic, the claim is valid.

Audit Report

## Title
`submit_delivery_receipt` pays relayer reward and settles pending orders without checking the decoded `DeliveryReceipt.success` flag - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

## Summary
`DeliveryReceipt::try_from` correctly decodes the `success: bool` field from the Ethereum Gateway's `InboundMessageDispatched(uint64 nonce, bytes32 topic, bool success, bytes32 reward_address)` event log, but `Pallet::process_delivery_receipt` never reads `receipt.success` before calling `T::RewardPayment::register_reward` and removing the `PendingOrders` entry. A relayer can submit a valid proof for a Gateway event where the dispatched command failed on Ethereum (`success == false`) and still collect the reward while the pending order is permanently deleted.

## Finding Description
The extrinsic entry point verifies only the cryptographic validity of the event log/proof pair and decodes the receipt [4](#0-3) . `DeliveryReceipt::try_from` decodes all four fields of the Solidity event, including `success`, directly from the log [5](#0-4) . However, `process_delivery_receipt` only checks `receipt.gateway == T::GatewayAddress::get()` and `order.fee > 0` before unconditionally calling `register_reward` and `PendingOrders::remove(nonce)` — `receipt.success` is never referenced anywhere in this function [6](#0-5) . This confirms the field exists precisely to distinguish successful vs. failed dispatch on Ethereum, yet the settlement logic silently ignores it — an unchecked-result pattern structurally identical to ignoring an ERC20 `transfer` boolean return before crediting a withdrawal.

## Impact Explanation
Since `PendingOrders` is the sole bookkeeping for outstanding outbound message delivery/reward state, and it is unconditionally removed regardless of `success`, a relayer can obtain a reward for, and permanently erase the record of, a message whose command execution reverted on Ethereum. This causes duplicate/incorrect settlement (reward paid for a failed delivery) and permanent loss of the chain's record of an outstanding cross-chain obligation, matching the "duplicate settlement or payout" / "permanent bridge-state lock" impact categories.

## Likelihood Explanation
`submit_delivery_receipt` requires only `ensure_signed` — any account can call it, not just a privileged/trusted relayer [7](#0-6) . Constructing the proof for a genuine `InboundMessageDispatched(success=false)` log requires nothing beyond the same tooling used for legitimate delivery receipts, since the Gateway emits this event on both success and failure paths (the event schema itself carries the `success` flag for that reason).

## Recommendation
In `process_delivery_receipt`, branch on `receipt.success`: only call `T::RewardPayment::register_reward` and remove the `PendingOrders` entry when `receipt.success == true`. On `false`, either retain the order for retry, or transition it to an explicit failure-settlement path that does not pay the relayer reward but still safely clears/reconciles the queue state without rewarding non-delivery.

## Proof of Concept
1. A message is queued via `do_process_message`, creating a `PendingOrders[nonce]` entry with `fee > 0`.
2. The corresponding command fails to execute on the Ethereum Gateway, but the Gateway still emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. A relayer builds the standard event-log + proof pair for this log and calls `submit_delivery_receipt`.
4. `T::Verifier::verify` succeeds, `DeliveryReceipt::try_from` decodes `success: false` correctly, but `process_delivery_receipt` proceeds unconditionally: `register_reward` is called and `PendingOrders::remove(nonce)` executes, exactly as in the passing tests at `cumulus/.../snowbridge_v2_outbound.rs` lines 496-513 which only exercise `success: true`; a unit test asserting `success: false` still pays and removes the order would demonstrate the bug (equivalent to editing `mock_valid_event_proof`/`DeliveryReceipt` fixtures to set `success: false` and asserting `RewardRegistered` still fires and `PendingOrders::get(nonce)` still returns `None`).

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

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/delivery_receipt.rs (L35-51)
```rust
impl TryFrom<&Log> for DeliveryReceipt {
	type Error = DeliveryReceiptDecodeError;

	fn try_from(log: &Log) -> Result<Self, Self::Error> {
		let topics: Vec<B256> = log.topics.iter().map(|x| B256::from_slice(x.as_ref())).collect();

		let event = InboundMessageDispatched::decode_raw_log_validate(topics, &log.data)
			.map_err(|_| DeliveryReceiptDecodeError::DecodeLogFailed)?;

		Ok(Self {
			gateway: log.address,
			nonce: event.nonce,
			topic: H256::from_slice(event.topic.as_ref()),
			success: event.success,
			reward_address: event.reward_address.0,
		})
	}
```

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound.rs (L496-513)
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
