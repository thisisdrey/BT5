## Analog Found: Reward paid and order settled regardless of Ethereum message dispatch outcome

### Title
`process_delivery_receipt` ignores the `success` field of the Ethereum `InboundMessageDispatched` event, paying relayer rewards and settling pending orders even for failed message dispatches - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
The Snowbridge outbound-queue-v2 pallet's `submit_delivery_receipt` extrinsic verifies an Ethereum event log and decodes it into a `DeliveryReceipt`, which explicitly carries a `success: bool` field taken from the Gateway contract's `InboundMessageDispatched(nonce, topic, success, reward_address)` event [1](#0-0) . However, `process_delivery_receipt` never reads or checks `receipt.success` before paying the relayer reward and removing the pending order [2](#0-1) . This mirrors the ThorChain bug exactly: a contract-emitted status flag meant to signal whether the underlying action actually succeeded is disregarded by the consuming logic, causing the system to treat failed executions as successful settlements.

### Finding Description
`submit_delivery_receipt` verifies the raw event log via `T::Verifier::verify`, then decodes it into `DeliveryReceipt::try_from(&event.event_log)` [3](#0-2) . The decode extracts `gateway`, `nonce`, `topic`, `success`, and `reward_address` straight from the Solidity event `InboundMessageDispatched(uint64 indexed nonce, bytes32 topic, bool success, bytes32 reward_address)` [4](#0-3) .

`process_delivery_receipt` then:
1. Checks the gateway address matches.
2. Resolves the reward account.
3. Looks up the `PendingOrder` by nonce.
4. Unconditionally calls `T::RewardPayment::register_reward(...)` if `order.fee > 0`.
5. Removes the `PendingOrder` and emits `MessageDelivered`.

At no point is `receipt.success` inspected [2](#0-1) . A grep across the pallet confirms `receipt.success`/`.success` is never referenced anywhere in `outbound-queue-v2`'s pallet logic, and the pallet's own test suite (`bridges/snowbridge/pallets/outbound-queue-v2/src/test.rs`) contains no test exercising `success: false` — all delivery-receipt fixtures set `success: true`.

This is the direct structural analog of the ThorChain finding: the Ethereum-side contract already computes and emits whether the dispatch actually succeeded (analogous to whether `to.send()` succeeded in `transferOut`), but the on-chain consumer of that event (`process_delivery_receipt`, analogous to the `smartcontract_log_parser`/network state update) blindly treats the event as proof of success and advances settlement state (reward payment + order removal) regardless.

### Impact Explanation
Any relayer can submit a valid delivery-receipt proof for a message whose execution failed on Ethereum (e.g., ran out of gas, reverted, or hit calldata/logic errors inside the Gateway's dispatch), and still collect the full relayer reward while the `PendingOrder` is cleared as if delivery succeeded. This breaks the intended invariant that "message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically" — payout advances even when execution did not succeed. This causes duplicate/unearned reward payout from bridge relayer funds and permanently loses the ability to retry or account for the failed dispatch, since the pending order is deleted unconditionally.

### Likelihood Explanation
This requires no privileged actor. Any unpermissioned account can call `submit_delivery_receipt` with a legitimately verifiable proof of a genuine (but failed) Ethereum execution — the Gateway contract emits `InboundMessageDispatched` with `success=false` on failed dispatch attempts as part of normal operation, so this scenario occurs naturally whenever an Ethereum-side command fails, not only through active exploitation. This makes the likelihood high; it can trigger silently in production whenever any outbound command execution fails on the Ethereum side.

### Recommendation
In `process_delivery_receipt`, branch on `receipt.success`: only call `T::RewardPayment::register_reward` when `receipt.success == true`; when `false`, either emit a distinct `MessageDeliveryFailed`/`MessageDispatchFailed` event without paying the reward, or reprocess/retry logic, while still removing (or otherwise finalizing) the `PendingOrder` in a way that reflects the true outcome — matching Recommendation #1 from the source report (only treat the operation as successful state when the underlying execution result confirms success).

### Proof of Concept
1. A message with nonce `N` and fee `F>0` is queued via `do_process_message`, creating `PendingOrders[N] = { nonce: N, fee: F, block_number }` [5](#0-4) .
2. On Ethereum, the Gateway attempts to dispatch the command for nonce `N` but it fails (e.g. reverts), and the Gateway contract still emits `InboundMessageDispatched(N, topic, success=false, reward_address)`.
3. A relayer obtains a valid receipt/execution proof for this real event log and calls `submit_delivery_receipt(origin, event)`.
4. `T::Verifier::verify` succeeds (the event genuinely occurred and is provable) and `DeliveryReceipt::try_from` decodes `success = false` correctly.
5. `process_delivery_receipt` is invoked; it never checks `receipt.success`, so it pays `T::RewardPayment::register_reward(&reward_account, ..., F)` and removes `PendingOrders[N]`, emitting `MessageDelivered { nonce: N }` — as if the dispatch had succeeded [6](#0-5) .
6. Result: relayer reward paid and order marked resolved for a message whose Ethereum-side execution actually failed, with no way to detect or recover the failure from on-chain state.

### Citations

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
