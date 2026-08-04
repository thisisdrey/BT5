Confirmed: `receipt.success` is decoded from the Ethereum event but never referenced anywhere else in `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`. The reward payout logic only checks `order.fee > 0`, not `receipt.success`.

### Title
Relayer reward is paid on Snowbridge outbound delivery even when Ethereum dispatch failed - ([File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs])

### Summary
`process_delivery_receipt` decodes a `DeliveryReceipt` from a verified Ethereum event log, which includes a `success: bool` field indicating whether the message actually executed successfully on the Gateway contract on Ethereum. This field is decoded but never checked before releasing the relayer reward, mirroring the external report's core pattern: a value that is nominally meaningful for correctness (`success`, analogous to `encryptedOutputs`) is present in the data structure but not bound into any invariant that gates the state transition (payment), so its actual content has no effect on the outcome.

### Finding Description
`DeliveryReceipt` is defined with a `success` field decoded straight from the `InboundMessageDispatched` Solidity event: [1](#0-0) 

The pallet's `submit_delivery_receipt` extrinsic verifies the log via `T::Verifier::verify` and decodes it into a `DeliveryReceipt`, then calls `process_delivery_receipt`: [2](#0-1) 

`process_delivery_receipt` only checks the gateway address and the pending order's nonce/fee before paying the reward — `receipt.success` is never inspected: [3](#0-2) 

The `PendingOrder` created in `do_process_message` only tracks `nonce`, `fee`, and `block_number` — there's no separate accounting path for failed vs. successful delivery: [4](#0-3) 

Because `success` is decoded from a legitimately verified proof, this is not a "malicious relayer forging data" issue (which would be out of scope) — the field is authentic Ethereum state, but the pallet fails to use it as an invariant gate. This matches the report's broken-invariant class: data that should participate in a settlement decision is decoded/logged but never actually enforced, so the on-chain outcome (reward payout) is decoupled from the real-world condition (successful message execution) it is supposed to represent.

### Impact Explanation
This falls under "duplicate settlement or payout... must only advance after decode, dispatch, execution, and settlement succeed atomically." Here settlement (fee/reward payout in `T::RewardPayment::register_reward`) advances regardless of whether Ethereum-side execution actually succeeded. Any message whose dispatch on the Gateway contract reverts (`success = false`) — due to insufficient gas, a reverting command, or destination-side failure — still allows the relayer to claim the full fee from `PendingOrders`, draining accumulated relayer rewards for work that produced no successful cross-chain effect for the user. Repeated failed dispatches (which cost the relayer far less gas than a successful complex command) let a relayer profit from underpriced/failed work at the expense of the reward pool, degrading the economic assumptions of the bridge's incentive design.

### Likelihood Explanation
High likelihood in principle: submitting a `submit_delivery_receipt` extrinsic is a public, unprivileged, permissionless call available to any relayer who can obtain a valid delivery proof — no admin/governance/validator/collator/leaked-key assumption is required, and no malicious peer/prover behavior is needed since the log itself is genuinely emitted by the Gateway contract (which emits `InboundMessageDispatched` with `success=false` on any command execution failure, per the event ABI). Any command that reverts on the destination (e.g., asset transfer failing due to insufficient balance/frozen account on Ethereum) generates a legitimately verifiable `success=false` receipt that still yields full payment.

### Recommendation
Gate reward payment on `receipt.success`. If `success == false`, either withhold the reward entirely, pay only a reduced "gas refund" portion (if the pallet intends to compensate for gas spent regardless of command outcome), or route the fee to a separate failure-handling path instead of `T::RewardPayment::register_reward` with the full `order.fee`. Document and enforce (in code, not just documentation) whether "success" is meant to affect payout, and add a test asserting that a `success=false` receipt does not release the full reward.

### Proof of Concept
1. A user sends a command via `EthereumOutboundQueueV2` that is valid at commit-time but is designed/likely to revert on the Ethereum Gateway (e.g., a downstream call reverts due to state changes between commitment and execution).
2. A relayer delivers the message to Ethereum; the Gateway contract emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. The relayer submits this legitimately-provable receipt via `submit_delivery_receipt`.
4. `T::Verifier::verify` succeeds (the log is authentic), `DeliveryReceipt::try_from` decodes `success: false` correctly.
5. `process_delivery_receipt` proceeds to `T::RewardPayment::register_reward(&reward_account, T::DefaultRewardKind::get(), order.fee)` unconditionally since only `order.fee > 0` is checked — as shown at: [5](#0-4) 
6. The relayer receives the full reward despite the message failing to execute on Ethereum.

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
