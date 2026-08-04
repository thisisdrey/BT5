## Finding

The Snowbridge outbound-queue-v2 delivery-receipt handler decodes a `success` flag from the Ethereum `InboundMessageDispatched` event but never inspects it before paying the relayer reward, which is the same class of bug as the ERC20 report: a payout/event is unconditionally emitted from data that itself encodes whether the underlying operation actually succeeded, and that signal is silently discarded.

### Title
Relayer reward paid unconditionally regardless of on-chain delivery `success` flag - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
`DeliveryReceipt` decoded from the Ethereum Gateway's `InboundMessageDispatched` log carries a `success: bool` field indicating whether the relayed message actually executed successfully on Ethereum. [1](#0-0)  `Pallet::process_delivery_receipt` never reads this field: it verifies the gateway address, looks up the `PendingOrders` entry by nonce, and unconditionally registers the reward and removes the order, purely based on `order.fee > 0`. [2](#0-1) 

### Finding Description
The comment in the module docs states the reward should be paid "when the message has been verified and executed" [3](#0-2) , i.e., payout is intended to be conditioned on successful *execution*, not merely on proof that a receipt log exists. The `success` field exists precisely to distinguish a message that was dispatched but reverted/failed on the Ethereum side from one that executed correctly. However `process_delivery_receipt` ignores `receipt.success` entirely and always calls `T::RewardPayment::register_reward` when `order.fee > 0`, then removes the `PendingOrders` entry regardless of outcome:

```rust
let order = <PendingOrders<T>>::get(nonce).ok_or(Error::<T>::InvalidPendingNonce)?;
if order.fee > 0 {
    T::RewardPayment::register_reward(&reward_account, T::DefaultRewardKind::get(), order.fee);
}
<PendingOrders<T>>::remove(nonce);
Self::deposit_event(Event::MessageDelivered { nonce });
``` [4](#0-3) 

This is the same broken invariant as the ERC20 `Transfer` bug: an event/settlement (`MessageDelivered` + reward registration) is emitted that does not correspond to the actual semantic outcome encoded in the same payload (`success`). The `PendingOrder` is irrevocably removed on any receipt submission, so there is no way to later retry, penalize, or reconcile a failed dispatch — the payout state advances even though "execution ... succeed[ing] atomically" (per the Pivots' explicit requirement for message queues and payout state) never happened.

### Impact Explanation
Any legitimate, permissionless relayer who submits a valid delivery receipt for a message that failed to execute on Ethereum (e.g., reverted due to insufficient gas, a downstream command failure, or any other on-chain execution failure that still results in the Gateway emitting `InboundMessageDispatched` with `success = false`) will still receive the full relayer fee from BridgeHub's reward pool. This drains the bridge reward budget for work that did not achieve its intended outcome, and permanently discards the `PendingOrder`, so there is no possibility of re-delivery, retry-accounting, or reconciliation for the failed message. This is an unbacked/duplicate-style payout — value leaves the reward pot without the corresponding successful bridge delivery it is meant to compensate — directly matching the "theft or unbacked mint... duplicate settlement or payout" and "receipts and payout state must only advance after ... execution ... succeed atomically" impact criteria.

### Likelihood Explanation
No malicious peer, relayer, validator, or governance action is required. `submit_delivery_receipt` is a public, unprivileged extrinsic callable by any signed account holding a valid Ethereum proof [5](#0-4) ; the triggering condition is simply that the relayed message's execution on Ethereum failed (a normal, expected occurrence for any bridge under gas-estimation error, target contract revert, or command failure), which the Gateway contract itself reports via `success = false`. Every honestly-behaving relayer of a genuinely-failed message will collect this bug's reward.

### Recommendation
Check `receipt.success` in `process_delivery_receipt` before paying the reward. On `success == false`, either withhold the reward entirely, pay a reduced/only-gas-cost portion, or keep the `PendingOrder` (or move it to a distinct "failed" bucket) so failed deliveries can be retried/reconciled instead of being paid out and discarded identically to successful ones.

### Proof of Concept
1. A message is enqueued via `do_process_message`, creating a `PendingOrder{nonce, fee, ..}` with `fee > 0`. [6](#0-5) 
2. The message is delivered to the Ethereum Gateway but its execution reverts/fails; the Gateway still emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. A relayer submits `submit_delivery_receipt` with the resulting proof; `Verifier::verify` succeeds (the log is real) and `DeliveryReceipt::try_from` decodes `success = false`.
4. `process_delivery_receipt` never inspects `success`, pays `order.fee` to the reward account via `T::RewardPayment::register_reward`, removes the `PendingOrder`, and emits `MessageDelivered { nonce }` — identical to the successful-delivery path shown in the integration test at `snowbridge_v2_outbound.rs` where a `DeliveryReceipt{success: true, ...}` triggers `RewardRegistered`. [7](#0-6)

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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L36-41)
```rust
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

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound.rs (L103-122)
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

		// Submit a delivery receipt
		assert_ok!(EthereumOutboundQueueV2::process_delivery_receipt(relayer, receipt));

		assert_expected_events!(
			BridgeHubWestend,
			vec![
				RuntimeEvent::BridgeRelayers(pallet_bridge_relayers::Event::RewardRegistered { .. }) => {},
			]
		);
	});
```
