The claim is confirmed by direct code inspection. The code exactly matches what's described in the report.

Audit Report

## Title
Relayer reward is paid and `PendingOrder` settled unconditionally regardless of the on-chain `InboundMessageDispatched.success` flag - (File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs)

## Summary
`DeliveryReceipt::try_from` correctly decodes the `success: bool` field from the Ethereum `InboundMessageDispatched` event log, but `process_delivery_receipt` never reads or branches on `receipt.success` before paying the relayer reward and removing the `PendingOrder`. As a result, a cross-chain command that reverted on the Ethereum Gateway is settled identically to one that succeeded, with the sender's attached fee fully consumed and no refund, retry, or distinguishing event.

## Finding Description
`submit_delivery_receipt` is a public, permissionless extrinsic callable by any signed account: it verifies the Merkle/beacon proof of the event log via `T::Verifier::verify`, decodes it into a `DeliveryReceipt` (which includes `success`), and calls `Self::process_delivery_receipt(relayer, receipt)`. [1](#0-0) 

`process_delivery_receipt` checks only `T::GatewayAddress::get() == receipt.gateway` and that a `PendingOrder` exists for `receipt.nonce`. It never inspects `receipt.success`. It unconditionally pays `order.fee` to the reward account via `T::RewardPayment::register_reward`, removes the `PendingOrder`, and emits `Event::MessageDelivered { nonce }`. [2](#0-1) 

The `DeliveryReceipt` struct and its `TryFrom<&Log>` decode `success` directly from the Solidity `InboundMessageDispatched(uint64 indexed nonce, bytes32 topic, bool success, bytes32 reward_address)` event, confirming the field is available but simply discarded downstream. [3](#0-2) 

The `PendingOrder` (containing `nonce`, `fee`, `block_number`) is created in `do_process_message` when the outbound message is committed, and is only ever resolved via `process_delivery_receipt`. [4](#0-3) 

Because the success flag is verifiably part of the proved event log (not forgeable by the relayer) but is ignored, the settlement logic cannot distinguish a genuine Ethereum-side revert from a successful dispatch — the reward is paid and the order closed in both cases.

## Impact Explanation
This falls under "duplicate settlement or payout" / underpriced-work-with-fund-loss in the Polkadot SDK impact gate: any legitimate Gateway-side revert (e.g., failed token unlock due to insufficient allowance, a reverting `Transact`/`Upgrade` command) still results in the sender's fee being fully paid to the relayer via `T::RewardPayment::register_reward`, with the `PendingOrder` permanently removed and no refund or retry path. This is reachable through the public, unprivileged `submit_delivery_receipt` extrinsic without requiring a malicious relayer, validator, or governance action — a normal, honest relayer submitting a valid proof of a legitimately failed dispatch triggers identical fund loss to the sender.

## Likelihood Explanation
High. Ethereum Gateway-side reverts are an expected, non-adversarial occurrence (insufficient balances/allowances, transient gas issues in nested `Transact` calls, contract-level checks failing). `submit_delivery_receipt` is the only mechanism to resolve a `PendingOrder`, so every legitimate revert results in this misallocated payout, and any relayer — honest or not — is incentivized to submit the proof regardless of `success`.

## Recommendation
Branch on `receipt.success` in `process_delivery_receipt`. On `false`, do not pay the reward as if the command succeeded; instead refund the fee to the original sender or route to a dedicated failed-delivery accounting path, and emit a distinguishable event (e.g., `MessageDeliveryFailed`) rather than unconditionally emitting `Event::MessageDelivered`.

## Proof of Concept
1. A message with `fee = F` is enqueued and committed, creating `PendingOrders[nonce] = PendingOrder{nonce, fee: F, block_number}` in `do_process_message`. [4](#0-3) 
2. The relayer executes the message on the Ethereum Gateway; the dispatched command reverts, so the Gateway emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. The relayer calls `submit_delivery_receipt` with a valid proof of this event; `T::Verifier::verify` succeeds and `DeliveryReceipt::try_from` decodes `success: false` correctly. [5](#0-4) 
4. `process_delivery_receipt` runs without checking `receipt.success`: it pays `order.fee` (`F`) to `reward_account`, removes `PendingOrders[nonce]`, and emits `Event::MessageDelivered { nonce }` — identical to the success case. [6](#0-5) 
5. The sender's fee `F` is fully spent even though the command never executed successfully on Ethereum, and no state remains to retry or refund it. This is systematically repeatable for every reverted command routed through the bridge.

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
