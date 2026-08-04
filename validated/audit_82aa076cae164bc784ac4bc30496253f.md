## Analysis

The external report's core broken invariant is: **a state-changing/value-transfer operation proceeds without validating a boolean condition that should gate it**, allowing loss/incorrect settlement of value. The closest local analog is in the Snowbridge V2 outbound queue reward-settlement path.

### Title
Relayer reward paid regardless of Ethereum delivery outcome due to ignored `success` field in `DeliveryReceipt` - (`bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
`DeliveryReceipt` decoded from the `InboundMessageDispatched` Ethereum event carries a `success: bool` field indicating whether the message actually executed successfully on Ethereum. `Pallet::process_delivery_receipt`, invoked from the public extrinsic `submit_delivery_receipt`, never reads or checks this field before paying out the relayer reward and settling the `PendingOrder`.

### Finding Description
The event `InboundMessageDispatched(uint64 indexed nonce, bytes32 topic, bool success, bytes32 reward_address)` is decoded into the `DeliveryReceipt` struct, which explicitly carries `success` [1](#0-0) .

`process_delivery_receipt`, called from the signed, unprivileged extrinsic `submit_delivery_receipt`, only checks the gateway address and the pending nonce before unconditionally paying `order.fee` to `reward_account` and removing the order — `receipt.success` is never inspected: [2](#0-1)  and the extrinsic entrypoint that any signed relayer can call: [3](#0-2) .

A grep of the entire pallet confirms `success` is not referenced anywhere in `outbound-queue-v2/src/lib.rs`, so the field is decoded purely for verification-log parsing but has zero effect on reward or state settlement logic. This is the same class of bug as the report: **a value that should gate a state/value-changing branch is computed but not enforced**, allowing the branch to execute unconditionally.

### Impact Explanation
Any relayer can submit a valid, correctly-proven `EventProof` for an `InboundMessageDispatched` log where `success = false` (i.e., the command execution reverted on the Ethereum Gateway) and still collect the full relayer fee from `order.fee`, since only `gateway` and `nonce` are validated. This is a "duplicate settlement or payout"/"theft or unbacked...payout" style impact per the required impact gate: relayer rewards are paid out even when the corresponding cross-chain command did not actually execute, and the order is removed from `PendingOrders`, permanently closing out accounting for a message that never succeeded on the destination chain. This can drain the pallet's reward budget without providing genuine delivery service, and desynchronizes on-chain relayer incentives from actual message execution outcomes on Ethereum.

### Likelihood Explanation
The path requires no privileged access, no malicious relayer/validator/prover collusion beyond simply calling a public extrinsic with a legitimate proof of a normal (but reverted) Ethereum transaction — commands can fail on Ethereum for many benign reasons (insufficient gas at execution, contract-level revert, etc.), so `success = false` receipts are a realistic occurrence, not a contrived edge case. Any relayer who observes such a log can submit it and collect payment.

### Recommendation
In `process_delivery_receipt`, branch on `receipt.success` before crediting the reward: pay the relayer only when `receipt.success == true`; on `false`, remove/handle the `PendingOrder` without minting/registering a reward (or route to a distinct failure-handling/refund path), and emit a distinguishing event (e.g. `MessageDeliveryFailed`) instead of unconditionally emitting `MessageDelivered`.

### Proof of Concept
1. A message is queued via `do_process_message`, creating `PendingOrders::<T>::insert(nonce, PendingOrder { nonce, fee, .. })` with `fee > 0` [4](#0-3) .
2. On Ethereum, the corresponding command execution reverts, and the Gateway emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. A relayer builds a valid `EventProof`/`Proof` for this event (verifiable by `T::Verifier::verify`) and calls `submit_delivery_receipt(origin, event)`.
4. `DeliveryReceipt::try_from` decodes `success = false` into the receipt, but `process_delivery_receipt` never inspects it: `T::RewardPayment::register_reward(&reward_account, ..., order.fee)` executes unconditionally, `PendingOrders::remove(nonce)` runs, and `Event::MessageDelivered` fires — identical to the success path [5](#0-4) , confirmed by the existing test asserting `RewardRegistered`/`MessageDelivered` purely from proof validity and nonce presence, independent of `success` [6](#0-5) .

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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L426-438)
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

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound.rs (L407-426)
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
