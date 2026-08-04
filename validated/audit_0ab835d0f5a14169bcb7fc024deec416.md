## Analysis

The TON report's core broken invariant is: **an outbound message that carries value/state to another contract is marked "executed/settled" the moment it is *sent*, without verifying that the counterpart actually accepted/processed it.** If the counterpart rejects it, the sender's state is already advanced and cannot be corrected.

The direct structural analog in this repository is Snowbridge's V2 outbound queue delivery-receipt handling.

### The pipeline

`Pallet::do_process_message` creates a `PendingOrder` for every outbound message and assigns it a fee/nonce [1](#0-0) . The message is later executed on Ethereum, and the Ethereum gateway contract emits an `InboundMessageDispatched(nonce, topic, success, reward_address)` event that explicitly records whether execution **succeeded or failed** [2](#0-1) .

A relayer submits this event as a `DeliveryReceipt` via the public, unprivileged extrinsic `submit_delivery_receipt`, which verifies the proof and calls `process_delivery_receipt`: [3](#0-2) 

```rust
pub fn process_delivery_receipt(
    relayer: <T as frame_system::Config>::AccountId,
    receipt: DeliveryReceipt,
) -> DispatchResult
{
    ensure!(T::GatewayAddress::get() == receipt.gateway, Error::<T>::InvalidGateway);
    let reward_account = ...;
    let nonce = receipt.nonce;
    let order = <PendingOrders<T>>::get(nonce).ok_or(Error::<T>::InvalidPendingNonce)?;
    if order.fee > 0 {
        T::RewardPayment::register_reward(&reward_account, T::DefaultRewardKind::get(), order.fee);
    }
    <PendingOrders<T>>::remove(nonce);
    Self::deposit_event(Event::MessageDelivered { nonce });
    Ok(())
}
``` [4](#0-3) 

`receipt.success` — the field that tells the pallet whether execution on Ethereum actually succeeded — **is never inspected**. The pallet checks only `receipt.gateway`, then unconditionally pays the relayer reward and unconditionally removes the `PendingOrder`, permanently marking the message as "delivered" via the `MessageDelivered` event, regardless of whether `success` was `true` or `false`.

This is exactly the TON `try_execute` pattern: the order/message is stamped as finalized ("executed"/"delivered") as soon as the outbound flow completes proof verification, without checking whether the downstream side actually accepted the operation. If the Ethereum-side dispatch reverts (analogous to the multisig bouncing the message — e.g., gateway paused, gas exhausted, an operator/channel-config change invalidating the command), `success` will be `false`, yet the pallet still pays the reward and irrevocably deletes `PendingOrders[nonce]`. There is no retry path once the order is removed, and the relayer is compensated for a delivery that did not actually execute.

### Title
Snowbridge outbound-queue-v2 pays relayer reward and finalizes delivery without checking `DeliveryReceipt::success` - (File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs)

### Summary
`process_delivery_receipt` decodes an Ethereum `InboundMessageDispatched` event into a `DeliveryReceipt` that carries an explicit `success: bool` flag, but the function ignores that flag entirely, rewarding the relayer and removing the `PendingOrder` whenever a valid, correctly-addressed receipt is submitted — even one reporting `success == false`.

### Finding Description
`do_process_message` stores a `PendingOrder{nonce, fee, block_number}` for every outbound command headed to Ethereum [1](#0-0) . Settlement of that order is meant to happen only once execution on Ethereum is confirmed, which is precisely why the Ethereum gateway's event includes a `success` boolean [2](#0-1) . The pallet's own integration test constructs receipts with an explicit `success: true` field, confirming that the field is meaningful and expected to be checked [5](#0-4) .

However, `process_delivery_receipt` only validates `receipt.gateway` against `T::GatewayAddress`, then pays the reward and deletes the order unconditionally [6](#0-5) . `receipt.success` is dead data as far as this function is concerned. Since `submit_delivery_receipt` is a plain signed extrinsic reachable by any relayer who can produce a valid inclusion proof for the emitted event [3](#0-2) , no privileged actor is required to trigger this — it fires on the normal, expected relayer flow whenever the Ethereum-side command reverts (e.g. paused gateway, insufficient gas allocation from `GasMeter`, stale/invalidated command state), which is a routine occurrence, not an adversarial one.

### Impact Explanation
Every failed/reverted cross-chain command still results in (a) the relayer being paid `order.fee` for a delivery that did not execute, and (b) `PendingOrders::remove(nonce)`, which permanently forecloses any retry or accounting reconciliation for that message. This is a duplicate/incorrect settlement: value (the reward) is paid without the corresponding execution having actually succeeded, and message/payout state advances to a terminal "delivered" state without execution success — the exact violation called out in the pivot: "Message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically."

### Likelihood Explanation
High. This does not require a malicious relayer, governance action, or privileged actor — a routine execution failure on the Ethereum side (which the protocol explicitly anticipates by including a `success` flag in the event) combined with any honest relayer submitting the resulting receipt triggers the bug deterministically.

### Recommendation
In `process_delivery_receipt`, branch on `receipt.success`: only pay the reward and remove the `PendingOrder` when `success == true`. When `success == false`, either retain the order for a retry/resubmission path or transition it to an explicit failed state, and emit a distinct event (e.g. `MessageDispatchFailed`) instead of `MessageDelivered`, mirroring the recommendation in the source report to define explicit failure handling rather than silently treating failure as success.

### Proof of Concept
1. A command is enqueued and processed via `do_process_message`, creating `PendingOrders[nonce]` with `fee > 0` [7](#0-6) .
2. The message is relayed to Ethereum but its execution reverts (e.g., gas limit computed by `GasMeter` insufficient, or gateway paused), so the emitted `InboundMessageDispatched` event carries `success = false`.
3. Any relayer submits `submit_delivery_receipt` with a valid proof of this event; `T::Verifier::verify` succeeds since the event is genuine, and `DeliveryReceipt::try_from` decodes `success: false` correctly.
4. `process_delivery_receipt` checks only `receipt.gateway`, pays `order.fee` to the reward account, removes `PendingOrders[nonce]`, and emits `MessageDelivered { nonce }` — identical to the success path — even though the command never executed on Ethereum [8](#0-7) .

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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L426-440)
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

			<Nonce<T>>::set(nonce);

			Self::deposit_event(Event::MessageAccepted { id, nonce });
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

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound.rs (L407-415)
```rust
		let relayer = BridgeHubWestendSender::get();
		let reward_account = AssetHubWestendReceiver::get();
		let receipt = DeliveryReceipt {
			gateway: EthereumGatewayAddress::get(),
			nonce: 1,
			reward_address: reward_account.into(),
			topic: H256::zero(),
			success: true,
		};
```
