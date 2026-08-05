This confirms the claim exactly as described. `process_delivery_receipt` reads `receipt.gateway`, `receipt.reward_address`, and `receipt.nonce`, but `receipt.success` is decoded in `DeliveryReceipt::try_from` and never referenced afterward — no `ensure!`, no branch, no conditional logic based on it anywhere in the function.This fully confirms the claim: `DeliveryReceipt::try_from` decodes `success: event.success` from the on-chain Ethereum event at line 48, but this field is never consulted in `Pallet::process_delivery_receipt`.

Audit Report

## Title
Relayer reward is paid regardless of message execution outcome because `DeliveryReceipt::success` is never checked - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

## Summary
The Ethereum `InboundMessageDispatched` event log includes a `success: bool` field indicating whether the relayed message actually executed on the Gateway contract, and this is faithfully decoded into `DeliveryReceipt::success` in `bridges/snowbridge/primitives/outbound-queue/src/v2/delivery_receipt.rs`. However, `Pallet::process_delivery_receipt` in `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs` never reads this field before paying out the relayer reward and removing the `PendingOrder`, so a legitimately-proven but failed dispatch is rewarded identically to a successful one.

## Finding Description
`submit_delivery_receipt` verifies the beacon/Merkle proof over the event log via `T::Verifier::verify`, decodes it into a `DeliveryReceipt` via `TryFrom<&Log>`, and forwards it to `process_delivery_receipt`: [1](#0-0) 

`DeliveryReceipt::try_from` correctly decodes all fields including `success` from the `InboundMessageDispatched` Solidity event: [2](#0-1) 

`process_delivery_receipt` then only checks `receipt.gateway` against `T::GatewayAddress`, resolves the `reward_account`, looks up the `PendingOrder` by `receipt.nonce`, and unconditionally pays `order.fee` via `T::RewardPayment::register_reward` before removing the order — `receipt.success` and `receipt.topic` are never read: [3](#0-2) 

The proof verification (`T::Verifier::verify`) only attests that the log was genuinely emitted by the real Gateway contract; it makes no statement about the value of the `success` field itself. A Gateway-emitted event with `success = false` (representing a genuine execution failure on Ethereum, e.g., a reverted command) is just as provable as one with `success = true`. Because the pallet logic never branches on this decoded value, both cases produce an identical outcome: reward payout and permanent removal of `PendingOrders::<T>` entry for that `nonce`.

## Impact Explanation
This is a public, underpriced/mis-costed work condition and duplicate/incorrect settlement: fees are paid out of the bridge's reward pool for message deliveries whose intended cross-chain effect did not actually occur on Ethereum. Since `<PendingOrders<T>>::remove(nonce)` executes unconditionally once proof verification succeeds — not conditioned on actual message execution success — there is no retry, refund, or reconciliation path once this happens; the fee is spent and the record is gone whether or not the user's cross-chain command actually executed.

## Likelihood Explanation
This is reachable by any unprivileged, honest relayer under normal network conditions: Ethereum-side command execution can fail for many benign reasons (insufficient gas budget provisioned, target contract revert, etc.), which is expected operational behavior rather than an attack. No malicious relayer, prover, or governance action is required — a completely legitimate proof for a real Gateway-emitted `success = false` event is sufficient to trigger full reward payment via the code path shown above.

## Recommendation
In `process_delivery_receipt`, branch on `receipt.success`: only invoke `T::RewardPayment::register_reward` when `receipt.success == true`. For `success == false`, add an explicit failure-handling path — e.g., emit a distinct `MessageDeliveryFailed` event and decide whether to remove the pending order without paying a reward, or implement retry/refund semantics — but the fee must never be paid out for a message whose Ethereum-side execution did not succeed.

## Proof of Concept
1. `do_process_message` enqueues a message and inserts a `PendingOrder { nonce, fee, .. }` into `PendingOrders`, as at `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs` lines 426-436.
2. On Ethereum, the Gateway contract's dispatch of the corresponding command reverts, so the Gateway emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. A relayer builds a legitimate beacon/Merkle proof for this real event log and calls `submit_delivery_receipt(origin, event)`.
4. `T::Verifier::verify` succeeds (the proof is valid), and `DeliveryReceipt::try_from` correctly decodes `success = false`.
5. `process_delivery_receipt` ignores `receipt.success`, pays `order.fee` to `reward_account` via `T::RewardPayment::register_reward`, removes the `PendingOrder`, and emits `MessageDelivered { nonce }` — an outcome indistinguishable from a genuinely successful delivery. A unit test asserting `assert_ok!(OutboundQueue::submit_delivery_receipt(...))` followed by `PendingOrders::<Test>::get(nonce).is_none()` would pass identically whether the mock event encodes `success = true` or `success = false`, confirming the field has no effect on pallet behavior.

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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L445-480)
```rust
		/// Process a delivery receipt from a relayer, to allocate the relayer reward.
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

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/delivery_receipt.rs (L10-51)
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

#[derive(Copy, Clone, Encode, Decode, Eq, PartialEq, Debug, TypeInfo)]
pub enum DeliveryReceiptDecodeError {
	DecodeLogFailed,
	DecodeAccountFailed,
}

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
