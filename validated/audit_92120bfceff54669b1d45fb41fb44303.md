## Title
Reward payout on delivery receipt ignores dispatch `success` flag, causing incorrect settlement regardless of actual message execution outcome - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

## Summary

### Finding Description
The Solana report's core broken invariant is: a validation mechanism that is supposed to gate an action on a specific real-world condition (post-instruction sequence actually having occurred) is bypassed because the field/flag meant to encode that condition is never actually checked, causing the action (updating obligation/reserve state) to be accepted as if the condition held.

The local analog is in `Pallet::process_delivery_receipt` in the Snowbridge outbound-queue-v2 pallet. The `DeliveryReceipt` decoded from the Ethereum `InboundMessageDispatched` event explicitly carries a `success: bool` field [1](#0-0) , which is populated from the on-chain event log during decoding [2](#0-1) . This field is meant to indicate whether the message dispatch on the Ethereum side actually succeeded.

However, `process_delivery_receipt` never inspects `receipt.success` before paying out the relayer reward and settling the order: [3](#0-2) 

It only checks the gateway address, resolves the `nonce` to look up the `PendingOrders` entry, and if `order.fee > 0` unconditionally calls `T::RewardPayment::register_reward`, then removes the order and emits `MessageDelivered`. There is no branch conditioned on `receipt.success`.

## Impact Explanation
This is analogous to the "post-instruction validation" bug: the sequence "message dispatched → dispatch succeeded on destination → reward paid, order settled" is what the design intends (per the module doc: "When the message has been verified and executed, the relayer will call... to fetch the pending order... pay reward"), but the actual code accepts *any* dispatch outcome (success or failure) as sufficient grounds to pay and settle. A relayer can submit a legitimately verified proof for a message whose Ethereum-side execution *failed* (`success = false`) and still receive the full fee reward, and the `PendingOrder` is removed either way. This causes duplicate/incorrect payout unconditioned on actual successful settlement — falling under "duplicate settlement or payout" / "message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically," per the impact gate.

## Likelihood Explanation
Likelihood is bounded by the fact that `receipt.success` still requires a valid Merkle/Beefy-verified proof (`T::Verifier::verify`) of a genuine `InboundMessageDispatched` event log from the real Gateway contract, so an attacker cannot forge the event — but they do not need to: any relayer that relays a message whose remote dispatch fails on Ethereum (e.g., due to insufficient gas, a reverting command, or another legitimate on-chain failure) will still trigger this code path and be rewarded, since `submit_delivery_receipt` is a public, unprivileged, signed extrinsic [4](#0-3) . No malicious peer, validator, or relayer collusion is required; a normal relayer submitting a real receipt for a failed dispatch is sufficient to trigger the mispayout.

I was not able to fully verify from the index whether `success` is intentionally deferred to a different code path (e.g. a separate handling elsewhere for failed dispatches) since the pallet is relatively new (v2) and cross-references were limited by index coverage; a Devin session with full repository access should confirm there is no other consumer of `receipt.success` before treating this as a confirmed bug.

## Recommendation
In `process_delivery_receipt`, branch on `receipt.success`:
- If `true`, proceed with the existing reward payment and order removal as today.
- If `false`, still remove/settle the `PendingOrder` (to avoid a permanent lock of that slot) but explicitly should not reward the relayer for a failed dispatch, or should route to a distinct failure-handling/refund flow, and should emit a distinguishing event (e.g. `MessageDispatchFailed`) instead of `MessageDelivered`.

## Proof of Concept
1. A message is queued via `do_process_message`, creating `PendingOrders[nonce]` with a positive `fee`.
2. The relayer relays it to Ethereum, but the destination command reverts/fails on execution (e.g., insufficient gas budget from `GasMeter`, or an application-level revert), causing the emitted `InboundMessageDispatched(nonce, topic, success=false, reward_address)` event.
3. The relayer submits `submit_delivery_receipt` with a valid proof of this event.
4. `T::Verifier::verify` succeeds (the event is real), `DeliveryReceipt::try_from` decodes `success = false` correctly.
5. `process_delivery_receipt` ignores `success`, finds `order.fee > 0`, and calls `T::RewardPayment::register_reward`, paying the relayer as if the dispatch had succeeded, then removes the order. [5](#0-4)

### Citations

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/delivery_receipt.rs (L14-27)
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
