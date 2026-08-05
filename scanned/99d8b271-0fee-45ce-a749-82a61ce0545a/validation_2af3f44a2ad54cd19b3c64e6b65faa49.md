## Finding

### Title
Relayer reward paid on `submit_delivery_receipt` regardless of on-Ethereum delivery outcome (`success` field ignored) - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
The `DeliveryReceipt` decoded from the Ethereum `InboundMessageDispatched` event carries a `success: bool` field, but `Pallet::process_delivery_receipt` never inspects it before paying out the relayer reward.

### Finding Description
`DeliveryReceipt` is decoded from the `InboundMessageDispatched(uint64 nonce, bytes32 topic, bool success, bytes32 reward_address)` Ethereum log [1](#0-0) . The field is populated from the verified log but `process_delivery_receipt` only checks `gateway` and looks up the `PendingOrder` by `nonce` — `receipt.success` is read into the struct but never branched on before paying the reward and removing the order: [2](#0-1) 

So whether the Gateway on Ethereum executed the commands successfully or the message dispatch failed on the Ethereum side, as long as a valid, verified `InboundMessageDispatched` log with a matching `nonce` exists, the relayer collects `order.fee` via `T::RewardPayment::register_reward` and the `PendingOrder` is removed — settlement happens unconditionally on proof validity, not on execution success.

### Impact Explanation
This breaks the "settlement advances only after decode, dispatch, execution succeed" invariant for bridge reward payout: a relayer can be rewarded even for messages whose execution on Ethereum reverted or otherwise failed, since the boolean outcome flag from the verified event is discarded. This does not directly mint or unlock protocol funds beyond the treasury/fee pool for that message, but it decouples reward payment from correct execution — a relayer has no incentive to ensure successful delivery once fee capture is guaranteed regardless of `success`, degrading bridge processing quality/incentive alignment (falls under "duplicate settlement or payout"/"public underpriced work that ... stalls bridge processing" class per the impact gate, since underpriced/unconditional reward payout for failed work is exactly what the gate targets).

### Likelihood Explanation
High feasibility for an unprivileged relayer: no special privileges are needed — any account can call the permissionless `submit_delivery_receipt` extrinsic with a legitimately verified event log (a message that failed on Ethereum still emits `InboundMessageDispatched` with `success = false`), and the pallet will still pay the fee, because the code path in `process_delivery_receipt` never gates on `receipt.success`.

### Recommendation
In `process_delivery_receipt`, branch on `receipt.success` before calling `T::RewardPayment::register_reward` — either withhold/reduce the reward on `false`, or route it to a distinct "failed delivery" accounting path, and emit a distinct event so downstream systems can differentiate paid-for-success from paid-for-attempt.

### Proof of Concept
1. A message is enqueued and gets a `PendingOrder { nonce, fee, .. }` via `do_process_message` [3](#0-2) .
2. On Ethereum, the Gateway attempts dispatch and emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)` (execution failed, e.g. out-of-gas on a command).
3. A relayer submits this log + proof via `submit_delivery_receipt`; `T::Verifier::verify` succeeds (the log is genuine), `DeliveryReceipt::try_from` decodes `success = false`.
4. `process_delivery_receipt` ignores `success`, looks up `PendingOrders::get(nonce)`, and unconditionally calls `T::RewardPayment::register_reward(&reward_account, .., order.fee)` then removes the order [4](#0-3) .
5. The relayer is paid the full fee even though the bridged message failed on Ethereum — confirmable by asserting `RewardPayment` was invoked identically for `success: true` and `success: false` receipts (the existing test suite only exercises the `success: true` path, e.g. [5](#0-4) , and no test asserts reward is withheld for `success: false`).

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

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound.rs (L409-415)
```rust
		let receipt = DeliveryReceipt {
			gateway: EthereumGatewayAddress::get(),
			nonce: 1,
			reward_address: reward_account.into(),
			topic: H256::zero(),
			success: true,
		};
```
