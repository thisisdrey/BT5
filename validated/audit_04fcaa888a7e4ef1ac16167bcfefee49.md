All code excerpts match the repository exactly. `DeliveryReceipt::try_from` decodes `success` from the `InboundMessageDispatched` event but the field is never consulted afterward. `process_delivery_receipt` only checks `gateway` and nonce existence before paying the reward via `T::RewardPayment::register_reward` and unconditionally removing the `PendingOrders` entry, confirming the claim is accurate. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) 

Audit Report

## Title
Outbound Queue V2 pays relayer reward and clears the pending order regardless of Ethereum-side execution `success` - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

## Summary
`Pallet::process_delivery_receipt` registers the relayer reward and permanently removes the `PendingOrders` entry for a nonce based solely on a `DeliveryReceipt` whose `gateway` matches and whose `nonce` exists in storage — it never inspects `receipt.success`, even though that field is decoded directly from the Ethereum `InboundMessageDispatched(nonce, topic, success, reward_address)` event log and exists specifically to signal whether the message's commands were actually executed successfully on Ethereum.

## Finding Description
`DeliveryReceipt::try_from` in `bridges/snowbridge/primitives/outbound-queue/src/v2/delivery_receipt.rs` (lines 38-51) decodes `success` from the ABI-encoded event log data into the `DeliveryReceipt.success` field. `submit_delivery_receipt` (lines 298-317 of `outbound-queue-v2/src/lib.rs`) verifies the storage/execution proof via `T::Verifier::verify`, decodes the log into a `DeliveryReceipt`, and forwards it unmodified to `process_delivery_receipt`. That function (lines 445-480) checks only `T::GatewayAddress::get() == receipt.gateway` and that `<PendingOrders<T>>::get(nonce)` returns `Some`, then unconditionally calls `T::RewardPayment::register_reward` for `order.fee` and unconditionally calls `<PendingOrders<T>>::remove(nonce)`. `receipt.success` is read during decode but never referenced in any subsequent conditional logic. The pallet's own module doc (lines 34-41) describes the intended flow as triggering only "When the message has been verified **and executed**," confirming the success check was intended but not implemented.

## Impact Explanation
If the Gateway contract's inbound command execution reverts or otherwise fails on Ethereum (e.g., insufficient gas budget relative to actual command cost), `InboundMessageDispatched` is still emitted with `success = false`. A relayer submitting that receipt is paid the full `order.fee` via `T::RewardPayment::register_reward`, and the `PendingOrders` entry is deleted with no retry/resend path, even though the intended asset unlock/mint/agent-call effect never occurred on Ethereum. This is a duplicate/wrongful settlement of a bridge reward without the corresponding successful outcome, and a permanent loss of the ability to retry or recover the failed cross-chain action — matching the "duplicate settlement or payout" and "permanent user-fund or bridge-state lock" impact categories.

## Likelihood Explanation
`submit_delivery_receipt` is a public, permissionless extrinsic callable by any signed relayer (`ensure_signed(origin)?`) requiring only a valid proof of an already-emitted event log; no governance, admin, or validator collusion is required. `success = false` events occur naturally whenever the committed gas from `T::GasMeter::maximum_dispatch_gas_used_at_most` is insufficient for the actual command execution, or can be induced by a relayer submitting the message with marginal gas on the Ethereum side, making the exploit condition realistically reachable and repeatable per-nonce.

## Recommendation
Check `receipt.success` in `process_delivery_receipt` before paying the reward. On `success == false`, withhold or reduce the reward, retain the `PendingOrders` entry (or move it to a distinct "failed" state) to allow retry/reprocessing or refund of the original sender, and emit a distinct failure event (e.g. `MessageDeliveryFailed`) instead of `MessageDelivered`.

## Proof of Concept
1. A message with a command whose actual Ethereum execution cost exceeds the gas committed via `OutboundCommandWrapper.gas` is queued, producing a `PendingOrder` with non-zero `fee` at a given `nonce`.
2. The relayer submits the message to the Gateway contract with only the committed gas — sufficient for the outer transaction/event emission to succeed, insufficient for the command logic to fully execute — causing `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. The relayer calls `submit_delivery_receipt` with a valid proof of that event log; `T::Verifier::verify` succeeds and `DeliveryReceipt::try_from` decodes `success: false`.
4. `process_delivery_receipt` checks only `gateway` and nonce existence, pays `order.fee` to `reward_account` via `T::RewardPayment::register_reward`, and calls `<PendingOrders<T>>::remove(nonce)` — despite `success == false`, permanently losing the ability to retry the message while paying out the reward for an unexecuted action.

### Citations

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/delivery_receipt.rs (L14-51)
```rust
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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L34-41)
```rust
//! 9. On the Ethereum side, the message root is ultimately the thing being verified by the Beefy
//!    light client.
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
