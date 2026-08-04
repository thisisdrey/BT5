## Analysis

The external report's core broken invariant is: **a boolean success/return value from an external call is silently ignored, so state advances (and value moves) as if the operation succeeded even when it did not.**

The closest real analog in this repository is in the Snowbridge V2 outbound queue's delivery-receipt processing, where the on-chain `success` flag from the relayed Ethereum event is decoded but never checked before the relayer reward is paid and the pending order is finalized.

### Title
Relayer reward is paid and pending order is finalized on delivery receipts without checking the `success` flag - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
`DeliveryReceipt` carries a `success: bool` field decoded straight from the Ethereum `InboundMessageDispatched(nonce, topic, success, reward_address)` event log [1](#0-0) , populated via `TryFrom<&Log>` [2](#0-1) . However, `process_delivery_receipt` never reads `receipt.success` — it only checks the gateway address, looks up the `PendingOrder` by `nonce`, unconditionally registers the reward, and removes the order: [3](#0-2) . This is functionally the same class of bug as calling `transfer()`/`transferFrom()` and not checking the returned boolean: the "did it actually succeed" signal is present in the data but is dropped, so the code proceeds as if success were guaranteed.

### Finding Description
`submit_delivery_receipt` verifies the Merkle/event proof and decodes the `DeliveryReceipt`, then calls `process_delivery_receipt` [4](#0-3) . Inside `process_delivery_receipt`:
- it fetches `order = PendingOrders::<T>::get(nonce)`,
- pays `T::RewardPayment::register_reward(&reward_account, ..., order.fee)` if `order.fee > 0`,
- removes the order and emits `Event::MessageDelivered`,

all without ever branching on `receipt.success` [5](#0-4) .

On the Ethereum Gateway side, `InboundMessageDispatched` is emitted whenever the message *dispatch* completes, but `success` reflects whether the *command execution* (e.g. `UnlockNativeToken`, `MintForeignToken`, `CallContract`) actually succeeded — a relayer controls the gas forwarded to the command execution when submitting the transaction on Ethereum, and can cause the inner command to revert/fail (out-of-gas for the sub-call, or a genuinely reverting `CallContract` target) while the outer dispatch still emits the event with `success = false`. Because the pallet treats any valid, well-formed `InboundMessageDispatched` log the same regardless of `success`, it:
1. Pays the relayer their `order.fee` reward for a message whose actual effect (token unlock/mint/call) never took place.
2. Deletes the `PendingOrder`, which is the only tracked bridge state for that nonce, permanently closing the settlement window — there is no retry path once the order is removed.

This violates the required pivot that "message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically" — here settlement state advances on dispatch alone, not on execution success.

### Impact Explanation
- Relayers are rewarded for message deliveries whose underlying command execution failed on Ethereum, i.e. payout for underpriced/incomplete work — a rational relayer can systematically under-gas message execution to farm rewards without completing the transfer/mint/unlock the message was meant to perform.
- Because `PendingOrders` is removed unconditionally, a message whose Ethereum-side effect failed (e.g. `UnlockNativeToken`/`MintForeignToken` never executed) is marked `MessageDelivered` and can never be resubmitted or retried, which can permanently strand the funds that were supposed to move via that command.
- This does not require a malicious/compromised relayer in the sense excluded by the impact gate — it requires only an economically rational relayer exploiting the protocol's own accounting gap by choosing gas parameters, which is a normal, unprivileged interaction with the public `submit_delivery_receipt` extrinsic.

### Likelihood Explanation
`submit_delivery_receipt` is a public, unsigned-origin-free (just `ensure_signed`) extrinsic that any relayer can call once they have a valid inclusion proof for *any* `InboundMessageDispatched` log, including ones with `success = false` [4](#0-3) . No additional privilege, governance action, or compromise is needed — only control over the gas parameter when the relayer itself submits the message to the Ethereum Gateway, which is entirely within a normal relayer's control.

### Recommendation
Check `receipt.success` in `process_delivery_receipt` before paying rewards and/or before removing the `PendingOrder`. On `success == false`, either: withhold/reduce the reward, keep the order available for a retried delivery, or route it to a distinct failure-handling path so that fund-moving commands that failed on Ethereum are not treated as settled.

### Proof of Concept
1. A message with a fee-bearing `PendingOrder` (nonce `N`) containing e.g. `UnlockNativeToken` is queued and committed on BridgeHub.
2. A relayer submits the corresponding Ethereum transaction to the Gateway but constrains the gas forwarded to the command execution so that the `UnlockNativeToken` sub-call reverts/fails, while the outer `InboundMessageDispatched` event is still emitted with `success = false, nonce = N`.
3. The relayer builds a proof for this legitimate (but `success:false`) event log and calls `submit_delivery_receipt` on BridgeHub.
4. `T::Verifier::verify` succeeds (it is a genuine log), `DeliveryReceipt::try_from` decodes `success = false` [6](#0-5) .
5. `process_delivery_receipt` ignores `success`, pays `order.fee` to the relayer, removes `PendingOrders[N]`, and emits `MessageDelivered` [5](#0-4) .
6. The token unlock never happened on Ethereum, the order can never be resubmitted, yet the relayer was paid and the protocol considers the message fully settled.

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
