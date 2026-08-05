This confirms the claim exactly. The `DeliveryReceipt` struct decoded from the `InboundMessageDispatched` Ethereum event log carries a `success: bool` field [1](#0-0) , populated correctly during proof decoding via `TryFrom<&Log>` [2](#0-1) . However, `process_delivery_receipt` only checks `receipt.gateway`, `receipt.reward_address`, and `receipt.nonce` — `receipt.success` is never read before the reward is registered and `PendingOrders` is removed [3](#0-2) . The extrinsic `submit_delivery_receipt` verifies the Merkle/event proof, decodes the receipt, and forwards it directly to `process_delivery_receipt` with no intervening success check [4](#0-3) .

Audit Report

## Title
Snowbridge `process_delivery_receipt` pays relayer reward and clears `PendingOrders` without validating `DeliveryReceipt::success` - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

## Summary
`Pallet::process_delivery_receipt` in the outbound-queue-v2 pallet registers a relayer reward and unconditionally removes the corresponding `PendingOrders` entry whenever `order.fee > 0`, without checking `receipt.success`. Since `DeliveryReceipt::success` reflects whether the message actually executed successfully on Ethereum, a failed delivery still results in the relayer being paid and the order record being permanently deleted.

## Finding Description
`submit_delivery_receipt` verifies the event proof, decodes it into a `DeliveryReceipt` (which includes `success: bool` sourced from the Ethereum `InboundMessageDispatched` event log), and passes it to `process_delivery_receipt` [4](#0-3) . Inside `process_delivery_receipt`, only `receipt.gateway`, `receipt.reward_address`, and `receipt.nonce` are consulted; `receipt.success` is never read. If `order.fee > 0`, `T::RewardPayment::register_reward` is called unconditionally, and `PendingOrders::<T>::remove(nonce)` deletes the only on-chain record that a reward is owed for that nonce, regardless of `success` [3](#0-2) . The `DeliveryReceipt` struct and its `TryFrom<&Log>` decoding confirm that `success` is populated straight from the on-chain event and is a first-class field intended to indicate delivery outcome [5](#0-4) . No other check in this code path gates on `success`.

## Impact Explanation
This is a duplicate/incorrect settlement bug: the pallet pays out `order.fee` and irreversibly clears the pending-order state even when the receipt's own `success` flag reports `false`, i.e., even when the message execution on Ethereum genuinely failed. This matches the "duplicate settlement or payout" and "message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically" pivot — settlement (reward registration + order removal) is decoupled from the receipt's success indicator, corrupting the payout/beneficiary-amount correctness invariant for the exact value `order.fee` tied to `nonce`.

## Likelihood Explanation
Any relayer whose submitted message execution on Ethereum reverts or fails (a normal, non-adversarial occurrence — e.g., gas issues, destination contract revert) can call the permissionless `submit_delivery_receipt` extrinsic with a genuinely valid Merkle/receipt proof for that failed-execution log. `T::Verifier::verify` only authenticates that the log exists on Ethereum; it performs no success-gating. Because `process_delivery_receipt` is the sole consumer of the decoded receipt for payout purposes and never inspects `receipt.success`, the missing check is reliably and repeatably reachable by any unprivileged, non-malicious relayer.

## Recommendation
In `process_delivery_receipt`, branch on `receipt.success` before calling `T::RewardPayment::register_reward`: on `false`, skip the reward registration (or route to a distinct failure-accounting path) and handle `PendingOrders` removal according to the intended failure policy, rather than always paying and clearing state as if delivery succeeded.

## Proof of Concept
1. Queue a message via the normal outbound flow, creating a `PendingOrders` entry with non-zero `fee` for `nonce = N` (via `do_process_message`) [6](#0-5) .
2. On Ethereum, the message execution reverts, emitting `InboundMessageDispatched(nonce=N, topic, success=false, reward_address)`.
3. A relayer obtains a valid proof for this log and calls `submit_delivery_receipt`, which verifies the proof, decodes `DeliveryReceipt{ success: false, ... }`, and calls `process_delivery_receipt(relayer, receipt)`.
4. `process_delivery_receipt` finds `order.fee > 0`, calls `T::RewardPayment::register_reward(&reward_account, ..., order.fee)`, then removes `PendingOrders::<T>::remove(N)` and emits `MessageDelivered { nonce: N }` — despite `success == false`.
5. A unit test asserting that `register_reward` is NOT invoked (or invoked with zero effect) when constructing a `DeliveryReceipt` with `success: false` would fail against current code, confirming the vulnerability.

### Citations

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
