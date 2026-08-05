## Analysis Summary

I examined several state-machine / async-callback patterns in the codebase that structurally resemble the SteadeFi bug (a settlement flow that reaches a "failure" signal but the handling code fails to branch on it, causing wrong finalization). The strongest, fully-verifiable local analog is in Snowbridge's outbound queue v2 delivery-receipt settlement path.

### Title
Outbound queue v2 pays relayer reward and closes the pending order without checking the delivery `success` flag - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
`DeliveryReceipt`, decoded from the Ethereum `InboundMessageDispatched` event, explicitly carries a `success: bool` field indicating whether the relayed message dispatch succeeded or failed on the Ethereum side. [1](#0-0) 
However, `Pallet::process_delivery_receipt` never reads or branches on `receipt.success` at all — it unconditionally pays the relayer reward and unconditionally removes the `PendingOrder`, regardless of whether the underlying delivery actually succeeded. [2](#0-1) 

### Finding Description
The comment block at the top of the pallet documents the intended flow: "When the message has been verified and executed, the relayer will call `submit_delivery_receipt`... Fetch the pending order by nonce of the message, pay reward with fee attached in the order... Remove the order." [3](#0-2) 

`submit_delivery_receipt` is a permissionless, signed extrinsic — any account can call it as long as it supplies a valid `EventProof` verified by `T::Verifier::verify`: [4](#0-3) 

Once the proof verifies and the log decodes into a `DeliveryReceipt`, `process_delivery_receipt` is invoked:
```rust
let order = <PendingOrders<T>>::get(nonce).ok_or(Error::<T>::InvalidPendingNonce)?;
if order.fee > 0 {
    T::RewardPayment::register_reward(&reward_account, T::DefaultRewardKind::get(), order.fee);
}
<PendingOrders<T>>::remove(nonce);
Self::deposit_event(Event::MessageDelivered { nonce });
``` [5](#0-4) 

`receipt.success` is decoded but discarded — the code path is identical whether the Ethereum `InboundMessageDispatched` event reports `success = true` or `success = false`. This is structurally the same class of bug as the SteadeFi report: an external system emits an authoritative status signal for a pending settlement (GMX "Cancelled" callback vs. Snowbridge `success:false` receipt), and the on-chain/pallet-side finalization logic fails to gate its accounting on that signal, so the "failure" case is silently treated identically to "success" and the pending state is torn down as if everything settled correctly.

### Impact Explanation
Because the reward is paid and the order permanently closed regardless of `success`, this breaks the invariant that "message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically." A relayer can be rewarded for a delivery that Ethereum itself recorded as a dispatch failure, and the protocol has no remaining record (`PendingOrders` entry removed) to reconcile, retry, or re-fee that message. This is a public, underpriced/incorrect-settlement path: value (relayer reward) is disbursed independent of the correctness condition the receipt was designed to convey, and there is no mechanism to reopen or re-route a failed delivery the way the pallet reopens overweight/failed message-queue items elsewhere in the runtime (e.g. `pallet-message-queue`'s explicit `Overweight`/execute_overweight retry path). [6](#0-5) 

### Likelihood Explanation
Any signed account can submit a delivery receipt as soon as a valid Ethereum inclusion proof exists for the `InboundMessageDispatched` log — no privileged relayer, governance, or admin role is required, and no malicious peer/validator collusion is needed; the attacker only needs a genuine (not forged) Ethereum event where the command execution failed (`success:false`), which is a normal occurrence (e.g., insufficient gas on the destination command, agent execution revert). This makes the condition realistically reachable through ordinary usage rather than an exotic or infrastructure-level compromise.

### Recommendation
Branch on `receipt.success` in `process_delivery_receipt`: on `false`, either skip/reduce the reward payment, keep (or transition) the `PendingOrder` into a distinct failure state for governance/relayer follow-up, and emit a `MessageDeliveryFailed`-style event instead of `MessageDelivered`, mirroring the explicit success/failure branching that `pallet-message-queue` and `pallet-migrations` already use for their pending/failed state transitions.

### Proof of Concept
1. A message is queued and gets a `PendingOrder { nonce, fee, .. }` via `do_process_message`. [7](#0-6) 
2. On Ethereum, the Gateway dispatches the inbound message but the inner command execution fails, so the emitted `InboundMessageDispatched` event has `success = false`.
3. Any signed account submits `submit_delivery_receipt` with the valid proof for this event. [4](#0-3) 
4. `process_delivery_receipt` reads `order.fee`, calls `T::RewardPayment::register_reward` unconditionally, and removes `PendingOrders[nonce]` — paying out and closing the order exactly as if `success` had been `true`. [5](#0-4) 

**Note on confidence:** I was not able to fully trace, within the available tool budget, whether `T::RewardPayment::register_reward` mints new balance versus draws from a pre-funded escrow, nor whether `success` is deliberately treated as "delivery occurred" rather than "command outcome" by design elsewhere in Snowbridge's documentation/runtime configuration. This affects the precise severity (unbacked mint vs. simple misallocation of already-escrowed fees) but not the core finding that the `success` field is decoded and then never consulted in the settlement logic shown above.

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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L36-42)
```rust
//! 10. When the message has been verified and executed, the relayer will call the extrinsic
//!     `submit_delivery_receipt` to:
//! 	a. Verify the message with proof for a transaction receipt containing the event log,
//! 	   same as the inbound queue verification flow
//! 	b. Fetch the pending order by nonce of the message, pay reward with fee attached in the order
//!    	c. Remove the order from `PendingOrders` map storage by nonce
//!
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

**File:** substrate/frame/message-queue/src/lib.rs (L795-813)
```rust
/// The status of an attempt to process a message.
#[derive(PartialEq)]
enum MessageExecutionStatus {
	/// There is not enough weight remaining at present.
	InsufficientWeight,
	/// There will never be enough weight.
	Overweight,
	/// The message was processed successfully.
	Processed,
	/// The message was processed and resulted in a, possibly permanent, error.
	Unprocessable { permanent: bool },
	/// The stack depth limit was reached.
	///
	/// We cannot just return `Unprocessable` in this case, because the processability of the
	/// message depends on how the function was called. This may be a permanent error if it was
	/// called by a top-level function, or a transient error if it was already called in a nested
	/// function.
	StackLimitReached,
}
```
