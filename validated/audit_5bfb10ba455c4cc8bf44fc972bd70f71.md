This confirms the claim exactly matches the current repository code. `process_delivery_receipt` reads `order.fee` and pays `T::RewardPayment::register_reward` unconditionally whenever `order.fee > 0`, without any reference to `receipt.success` anywhere in the function body [1](#0-0) . The `DeliveryReceipt` struct and its `TryFrom<&Log>` decoder do carry and populate the `success` field straight from the Ethereum `InboundMessageDispatched` event, but that field is only decoded — never consumed by any check in the payout path [2](#0-1) .Confirmed: `receipt.success` appears exactly once in `delivery_receipt.rs` (the field decode itself) and is never referenced anywhere else in the entire `bridges/snowbridge` tree, including `process_delivery_receipt` in `outbound-queue-v2/src/lib.rs`, which calls `register_reward` unconditionally on `order.fee > 0` [3](#0-2) . This validates the claim's central assertion that the `success` flag is decoded but discarded before payout.

Audit Report

## Title
Unconditional relayer reward payout ignoring on-chain delivery `success` flag in `submit_delivery_receipt` - ([File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs])

## Summary
`DeliveryReceipt::try_from` decodes the `success: bool` field directly from the Ethereum `InboundMessageDispatched(uint64 nonce, bytes32 topic, bool success, bytes32 reward_address)` event log, but `Pallet::process_delivery_receipt` never inspects `receipt.success` before calling `T::RewardPayment::register_reward` with the full `order.fee` and removing the `PendingOrder`. This allows a relayer to collect the full delivery fee for a message that reverted/failed on the Ethereum side, as long as they can produce a genuine, finalized `InboundMessageDispatched` log with `success == false`.

## Finding Description
`DeliveryReceipt::try_from` decodes `success` straight from the ABI-encoded event log with no further use of that value in the type itself [4](#0-3) . `Pallet::process_delivery_receipt` then only: checks `receipt.gateway == GatewayAddress`, resolves the `reward_account`, looks up `PendingOrders[nonce]`, and unconditionally pays `order.fee` via `T::RewardPayment::register_reward` whenever `order.fee > 0`, before removing the order [1](#0-0) . `T::Verifier::verify` only proves the log was genuinely included/finalized on Ethereum [5](#0-4)  — it does not prove that the underlying message execution succeeded. A grep across the entire `bridges/snowbridge` tree confirms `receipt.success`/`.success` is referenced only once, at the point of decode, and is never consumed in any payout-gating logic. This directly parallels the bridge-messages pallet pattern where rewards are paid only for messages that were newly and actually delivered/confirmed [6](#0-5) , a discipline absent here.

## Impact Explanation
The `PendingOrder.fee` is funded from real escrowed value (`Message.fee`) meant to compensate successful delivery [7](#0-6) . Paying this fee out for a message that failed on Ethereum (`success == false`) is an unbacked/unjustified payout that drains the fee escrow without the corresponding successful relaying service — a value-conservation violation where the bridge reward is not settled to the rightful condition (successful delivery only).

## Likelihood Explanation
`submit_delivery_receipt` is a fully public, unprivileged extrinsic gated only by `ensure_signed(origin)` [5](#0-4) . An attacker fully controls the Ethereum-side execution outcome (e.g., gas griefing or crafting an inherently reverting command) to produce a genuine `success == false` log, then relays that valid receipt to collect the fee — no privileged role, compromised relayer/prover, or off-repo infrastructure control is required.

## Recommendation
Gate `T::RewardPayment::register_reward` in `process_delivery_receipt` on `receipt.success == true`. For `success == false`, still remove the `PendingOrder` to release the escrow/stop further claims, but route the fee to a defined failure path (refund, burn, or retry) instead of paying it to the relayer/reward account.

## Proof of Concept
1. `do_process_message` enqueues a `Message`, inserting `PendingOrders[nonce] = { fee: F, ... }` [7](#0-6) .
2. An attacker delivers the corresponding message to the Ethereum Gateway, arranging for it to fail execution (insufficient gas relative to `command.gas`, or a reverting inner command); the Gateway still emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. The attacker obtains a valid receipts/beacon proof for this real, finalized log and calls `submit_delivery_receipt(event)`.
4. `T::Verifier::verify` succeeds; `DeliveryReceipt::try_from` decodes `success = false`, but this is never checked.
5. `process_delivery_receipt` reads `order.fee = F > 0` and calls `T::RewardPayment::register_reward(&reward_account, ..., F)`, then removes the order — the reward is paid despite `success == false`. A regression test asserting `register_reward` is not invoked when `receipt.success == false` would fail against current code.

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

**File:** bridges/modules/messages/src/lib.rs (L386-411)
```rust
			// mark messages as delivered
			let mut lane = any_state_outbound_lane::<T, I>(lane_id)?;
			let last_delivered_nonce = lane_data.last_delivered_nonce();
			let confirmed_messages = lane
				.confirm_delivery(
					relayers_state.total_messages,
					last_delivered_nonce,
					&lane_data.relayers,
				)
				.map_err(Error::<T, I>::ReceptionConfirmation)?;

			if let Some(confirmed_messages) = confirmed_messages {
				// emit 'delivered' event
				let received_range = confirmed_messages.begin..=confirmed_messages.end;
				Self::deposit_event(Event::MessagesDelivered {
					lane_id: lane_id.into(),
					messages: confirmed_messages,
				});

				// if some new messages have been confirmed, reward relayers
				let actually_rewarded_relayers = T::DeliveryConfirmationPayments::pay_reward(
					lane_id,
					lane_data.relayers,
					&confirmation_relayer,
					&received_range,
				);
```
