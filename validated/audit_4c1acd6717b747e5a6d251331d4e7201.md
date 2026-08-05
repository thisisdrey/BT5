This confirms the claim precisely. The code at `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs` lines 446-480 shows `process_delivery_receipt` never reads `receipt.success` — it only checks `receipt.gateway`, resolves `reward_account`, looks up `PendingOrder` by `receipt.nonce`, and pays `order.fee` unconditionally via `T::RewardPayment::register_reward` whenever `order.fee > 0`, then removes the order regardless of delivery outcome.Everything confirms the claim exactly as stated. The `DeliveryReceipt` struct decodes `success` directly from the `InboundMessageDispatched` event log at `bridges/snowbridge/primitives/outbound-queue/src/v2/delivery_receipt.rs` lines 16-27 and 44-50, and `Pallet::process_delivery_receipt` at `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs` lines 446-480 never references `receipt.success` in its logic — it unconditionally pays `order.fee` via `T::RewardPayment::register_reward` and removes the `PendingOrder` regardless of whether the Ethereum-side dispatch succeeded or failed.

Audit Report

## Title
Unconditional relayer reward payout ignoring on-chain delivery `success` flag in `submit_delivery_receipt` - ([File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs])

## Summary
`process_delivery_receipt` in the Snowbridge V2 outbound queue pallet pays the full `order.fee` to the `reward_account` for any nonce with a matching `PendingOrder`, without ever checking `receipt.success`, which is decoded straight from the Ethereum `InboundMessageDispatched` event log. This lets an attacker deliberately cause a message to fail execution on the Gateway contract (e.g., by under-providing gas or crafting a reverting command) and still relay the resulting failure log to collect the full relaying fee meant to compensate successful delivery.

## Finding Description
`DeliveryReceipt::try_from` decodes `success` directly from the Ethereum log with no further use of that field downstream: [1](#0-0) 

`submit_delivery_receipt` verifies the beacon/receipts proof, decodes the receipt, and forwards it to `process_delivery_receipt` with no gating logic of its own: [2](#0-1) 

`process_delivery_receipt` only checks the gateway address, resolves the reward account, looks up the `PendingOrder` by nonce, and pays `order.fee` unconditionally if it's greater than zero — `receipt.success` is never read: [3](#0-2) 

`T::Verifier::verify` only proves that the log was genuinely included and finalized on Ethereum, not that the underlying command succeeded — a legitimately-emitted log with `success == false` passes verification just as easily as one with `success == true`, so the existing proof check provides no protection against this gap.

## Impact Explanation
Every `PendingOrder.fee` is escrowed value tied to a specific message (set from `Message.fee` at enqueue time, as seen in `do_process_message` at lines 426-436 of the same file) meant to reward successful delivery. Paying it out for messages whose Ethereum-side execution failed is an unbacked reward disbursement that drains the fee escrow without the corresponding service being rendered, violating the "bridge rewards settle exactly once to the rightful beneficiary for genuine work" invariant.

## Likelihood Explanation
`submit_delivery_receipt` is a fully public, unprivileged extrinsic gated only by `ensure_signed(origin)`, and `success` is influenceable by any Ethereum account triggering a reverting/failing dispatch. No privileged role, compromised relayer, or off-repo assumption is needed beyond an ordinary unprivileged actor submitting an Ethereum transaction and relaying its valid receipt log.

## Recommendation
Gate the reward payment on `receipt.success`: only call `T::RewardPayment::register_reward` when `receipt.success == true`. For `false` receipts, still remove the `PendingOrder` to release the escrow and prevent further claims, but route the fee to a defined failure path (refund, burn, or retry) instead of paying a delivery reward.

## Proof of Concept
1. A `Message` is enqueued via `do_process_message`, creating `PendingOrders[nonce] = { fee: F, ... }`.
2. A relayer or attacker delivers the message to the Ethereum Gateway but causes it to fail execution (insufficient gas relative to `command.gas`, or an inherently reverting inner command). The Gateway still emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. The relayer obtains a valid receipts/beacon proof for this finalized log and calls `submit_delivery_receipt(event)`.
4. `T::Verifier::verify` succeeds; `DeliveryReceipt::try_from` decodes `success = false`, but `process_delivery_receipt` never checks it.
5. `process_delivery_receipt` reads `PendingOrders[nonce]`, sees `fee = F > 0`, calls `T::RewardPayment::register_reward(&reward_account, ..., F)`, then removes the order — the reward is paid despite `success == false`.

### Citations

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
