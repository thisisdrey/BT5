Verified: `PendingOrder` (defined in `bridges/snowbridge/pallets/outbound-queue-v2/src/types.rs` lines 16-24) stores only `nonce`, `block_number`, and `fee` — it does **not** retain the `topic` that was committed into `Messages`/`MessageLeaves` for that nonce. `process_delivery_receipt` (lines 446-480 of `lib.rs`) only checks `receipt.gateway` against `T::GatewayAddress` and looks up `PendingOrders` by `nonce`; it never compares `receipt.topic` against any stored value, and never checks `receipt.success` before calling `T::RewardPayment::register_reward`. This matches the claim exactly.

Audit Report

## Title
`process_delivery_receipt` pays relayer rewards without binding `DeliveryReceipt.topic` to the committed `OutboundMessage` and without checking `receipt.success` - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

## Summary
`Pallet::process_delivery_receipt` looks up `PendingOrders` solely by `receipt.nonce` and unconditionally pays `order.fee` to `reward_account`, without ever comparing `receipt.topic` to the `topic` originally committed for that nonce in `do_process_message`, and without gating payment on `receipt.success == true`. Because `PendingOrder` (in `types.rs`) does not even store the `topic` field, there is no way to perform this cross-check as currently structured.

## Finding Description
In `do_process_message`, an `OutboundMessage { origin, nonce, topic: id, commands }` is committed to `Messages`/`MessageLeaves`, and a `PendingOrder { nonce, fee, block_number }` is stored keyed by `nonce` only [1](#0-0) . The `PendingOrder` struct itself has no `topic` field [2](#0-1) .

`submit_delivery_receipt` verifies the Ethereum log/proof via `T::Verifier::verify` (proving the log's existence on a finalized Ethereum block) and decodes it into `DeliveryReceipt { gateway, nonce, topic, success, reward_address }` via `TryFrom<&Log>`, trusting the emitted event's `topic`/`success`/`reward_address` fields as-is [3](#0-2) [4](#0-3) .

`process_delivery_receipt` then only validates `receipt.gateway` and fetches `PendingOrders::get(nonce)`; it pays `order.fee` via `T::RewardPayment::register_reward` whenever `order.fee > 0`, regardless of `receipt.success`, and never re-derives or checks `receipt.topic` against anything [5](#0-4) . This confirms the claim: the only correlating field between the committed `OutboundMessage` and the relayed `DeliveryReceipt` is `nonce`; neither `topic` nor `success` gate the payout.

## Impact Explanation
Reward payment is not conditioned on `receipt.success`. Since the module doc explicitly states delivery outcome is reported through `success` in `InboundMessageDispatched`, a legitimately-emitted event with `success=false` (e.g., the Gateway's nested command execution reverted) still results in the full `order.fee` being paid out and the order being removed with a `MessageDelivered` event, as if delivery had fully succeeded. This violates the invariant that bridge reward/payout state must "only advance after decode, dispatch, execution, and settlement succeed atomically." Additionally, because `PendingOrder` never stores `topic`, there is no defense-in-depth correlation check between the committed message content and the relayed receipt for that nonce — the structural gap the claim describes.

## Likelihood Explanation
The path is reachable via the public, unprivileged `submit_delivery_receipt` extrinsic, which any signed account can call once holding a valid execution/receipt proof for a real on-chain Ethereum event. No governance, validator, or relayer-privilege escalation is needed; `success=false` deliveries are a normal, expected outcome of Gateway message execution (not merely a hypothetical contract bug), making the missing `success` check readily and repeatably triggerable in production operation.

## Recommendation
Add the committed `topic` to `PendingOrder` when created in `do_process_message`, and assert `receipt.topic == order.topic` in `process_delivery_receipt` before paying. Additionally, condition reward payment on `receipt.success == true`; for `success == false`, avoid rewarding a failed dispatch (e.g., emit a distinct failure event and/or route to a separate settlement path) instead of unconditionally paying the full fee and removing the order as if delivered successfully.

## Proof of Concept
1. `do_process_message` commits `OutboundMessage { origin: O, nonce: N, topic: T, commands: C }` and stores `PendingOrder { nonce: N, fee: F }` (no `topic`) [6](#0-5) .
2. On Ethereum, the Gateway processes message `N` but the nested command execution fails, so it legitimately emits `InboundMessageDispatched(nonce=N, topic=T, success=false, reward_address=R)`.
3. A relayer obtains a valid proof for this real log and calls `submit_delivery_receipt`; `T::Verifier::verify` succeeds and `DeliveryReceipt::try_from` decodes `success=false` [7](#0-6) .
4. `process_delivery_receipt` finds `PendingOrders::get(N)`, pays `order.fee` to `R` despite `success=false`, removes the order, and emits `MessageDelivered { nonce: N }` [8](#0-7)  — a unit test asserting reward payment for a `DeliveryReceipt` with `success: false` reproduces this.

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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L390-436)
```rust
			let outbound_message = OutboundMessage {
				origin,
				nonce,
				topic: id,
				commands: commands.clone().try_into().map_err(|_| {
					Self::deposit_event(Event::MessageRejected {
						id: Some(id),
						payload: message.to_vec(),
						error: Corrupt,
					});
					Corrupt
				})?,
			};
			Messages::<T>::append(outbound_message);

			// Convert it to an OutboundMessageWrapper (in ABI format), hash it using Keccak256 to
			// generate a committed hash, and store it in MessageLeaves storage which can be
			// verified on Ethereum later.
			let abi_commands: Vec<CommandWrapper> = commands
				.into_iter()
				.map(|command| CommandWrapper {
					kind: command.kind,
					gas: command.gas,
					payload: Bytes::from(command.payload),
				})
				.collect();
			let committed_message = OutboundMessageWrapper {
				origin: FixedBytes::from(origin.as_fixed_bytes()),
				nonce,
				topic: FixedBytes::from(id.as_fixed_bytes()),
				commands: abi_commands,
			};
			let message_abi_encoded_hash =
				<T as Config>::Hashing::hash(&committed_message.abi_encode());
			MessageLeaves::<T>::append(message_abi_encoded_hash);

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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/types.rs (L15-24)
```rust
#[derive(Encode, Decode, TypeInfo, Clone, Eq, PartialEq, Debug, MaxEncodedLen)]
pub struct PendingOrder<BlockNumber> {
	/// The nonce used to identify the message
	pub nonce: u64,
	/// The block number in which the message was committed
	pub block_number: BlockNumber,
	/// The fee in Ether provided by the user to incentivize message delivery
	#[codec(compact)]
	pub fee: u128,
}
```

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/delivery_receipt.rs (L10-52)
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
}
```
