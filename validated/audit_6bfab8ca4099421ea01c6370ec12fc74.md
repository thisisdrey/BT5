### Title
`process_delivery_receipt` pays relayer reward without checking the decoded `success` field of the delivery receipt - ([File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs])

### Summary
The Ethereum `Gateway` contract emits `InboundMessageDispatched(nonce, topic, success, reward_address)`, and this event is decoded into `DeliveryReceipt { gateway, nonce, topic, success, reward_address }` [1](#0-0) . The pallet decodes and carries this `success` boolean through `TryFrom<&Log>` [2](#0-1) , but `process_delivery_receipt` never inspects `receipt.success` before paying the relayer reward and clearing the pending order — it only checks the gateway address and nonce existence, then unconditionally calls `T::RewardPayment::register_reward` [3](#0-2) . This mirrors the reported class of bug: a verification/result field (the pairing-check result in the external report, here the on-chain execution "success" outcome) is computed and available but is never checked before the code proceeds to the "success"/payout path.

### Finding Description
`submit_delivery_receipt` verifies only that the event log is authentically included in an Ethereum block (via `T::Verifier::verify`, which checks the receipt Merkle/RLP inclusion proof and beacon finality) [4](#0-3) . That verification only proves the log *exists* in a finalized Ethereum block — it says nothing about whether the message dispatch on Ethereum actually *succeeded*. That semantic information is carried exclusively in the `success` boolean of the decoded `DeliveryReceipt`.

`process_delivery_receipt` then does:
```
ensure!(T::GatewayAddress::get() == receipt.gateway, Error::<T>::InvalidGateway);
...
let order = <PendingOrders<T>>::get(nonce).ok_or(Error::<T>::InvalidPendingNonce)?;
if order.fee > 0 {
    T::RewardPayment::register_reward(&reward_account, T::DefaultRewardKind::get(), order.fee);
}
<PendingOrders<T>>::remove(nonce);
Self::deposit_event(Event::MessageDelivered { nonce });
``` [5](#0-4) 

No branch on `receipt.success` exists anywhere in this pallet — a repo-wide search for `.success` usage inside `outbound-queue-v2` returns zero matches outside the struct definition itself. This is functionally identical to the reported Verifier.sol bug: an execution-status/result value is decoded and stored (or available), but the code advances to the reward/settlement path regardless of whether that value indicates true success or failure.

### Impact Explanation
Any relayer can submit a `DeliveryReceipt` derived from a genuine, finalized Ethereum log where the Gateway contract emitted `success=false` (e.g., the inbound message execution reverted or failed on the Ethereum side for reasons unrelated to relaying, such as insufficient gas supplied by the relayer, a reentrancy guard, or a downstream XCM decode/execution failure recorded as `success=false` by the contract) and still collect the full relayer reward (`order.fee`) as if delivery had fully succeeded. Because `PendingOrders` is removed unconditionally, there is also no path to retry or distinguish failed deliveries, causing duplicate/incorrect settlement of relayer rewards (public underpriced/over-rewarded work) and, depending on downstream accounting, a mismatch between what BridgeHub believes was delivered successfully and what actually executed on Ethereum.

### Likelihood Explanation
The path is reachable by any signed account (`ensure_signed(origin)`) with a legitimately finalized Ethereum log — no privileged relayer, validator, or governance action is required [6](#0-5) . The only "difficulty" is that `success=false` must occur naturally on the Ethereum side (which does happen — e.g., insufficient relayer-supplied gas at execution, or command execution errors that the Gateway contract catches and reports as `success=false`), not that the relayer must forge anything. This is a plausible, unprivileged public-entrypoint path.

### Recommendation
Branch on `receipt.success` in `process_delivery_receipt`: only call `T::RewardPayment::register_reward` when `receipt.success == true`; for `success == false`, either withhold/reduce the reward, emit a distinct `MessageDeliveryFailed` event, or otherwise handle failed dispatch (e.g., feed back to a retry/refund mechanism) instead of unconditionally paying the full fee and clearing the order as if it succeeded.

### Proof of Concept
1. A relayer relays a message; BridgeHub inserts a `PendingOrder { nonce, fee, .. }`.
2. On Ethereum, the Gateway dispatches the message but the receiving handler reverts/fails for a reason the contract catches, so the contract emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. The relayer takes this finalized transaction receipt/log and calls `submit_delivery_receipt` with a proof of inclusion.
4. `T::Verifier::verify` succeeds (the log is genuinely included and finalized) [7](#0-6) .
5. `DeliveryReceipt::try_from` decodes `success: false` correctly [8](#0-7) .
6. `process_delivery_receipt` ignores `success`, pays `order.fee` to `reward_account`, and removes the order [9](#0-8)  — full reward collected despite `success=false`.

Existing integration tests (`snowbridge_v2_outbound.rs`) only construct receipts with `success: true` and never exercise the `success: false` branch, so this gap has no test coverage confirming correct behavior [10](#0-9) .

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

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound.rs (L409-418)
```rust
		let receipt = DeliveryReceipt {
			gateway: EthereumGatewayAddress::get(),
			nonce: 1,
			reward_address: reward_account.into(),
			topic: H256::zero(),
			success: true,
		};

		// Submit a delivery receipt
		assert_ok!(EthereumOutboundQueueV2::process_delivery_receipt(relayer, receipt));
```
