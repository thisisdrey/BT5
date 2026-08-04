Confirmed: `DeliveryReceipt` decoded from the Ethereum `InboundMessageDispatched` event carries a `success: bool` field [1](#0-0) , but `Pallet::process_delivery_receipt` in the outbound-queue-v2 pallet never reads `receipt.success` — it pays the relayer as soon as the nonce is found in `PendingOrders`, unconditionally on the outcome of the dispatch on Ethereum.

### Title
Snowbridge outbound-queue-v2 pays relayer reward regardless of delivery success (`DeliveryReceipt::success` never checked) - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
`Pallet::process_delivery_receipt` decodes a `DeliveryReceipt` from an Ethereum event log that explicitly encodes whether the inbound message dispatch succeeded (`success: bool`), but the payout logic only checks `order.fee > 0` and never checks `receipt.success` before calling `T::RewardPayment::register_reward`. This mirrors the reported `SPBinaryPrompt::getScore` bug: a payout path that rewards the caller based on a claim/prediction (here, "I relayed this message") without checking the correctness/outcome flag that the same data structure carries (`answerAIsWinner == reactions[...].answer` in the original vs. `receipt.success` here).

### Finding Description
The extrinsic flow is:
1. `submit_delivery_receipt` verifies the Merkle/Beefy proof of the Ethereum event log via `T::Verifier::verify` [2](#0-1) .
2. It decodes the log into a `DeliveryReceipt` via `TryFrom<&Log>`, which populates `success` straight from the Solidity event `InboundMessageDispatched(uint64 nonce, bytes32 topic, bool success, bytes32 reward_address)` [3](#0-2) .
3. `process_delivery_receipt` then only checks `T::GatewayAddress::get() == receipt.gateway` and that a `PendingOrder` exists for `receipt.nonce`, and pays `order.fee` to the reward account if `order.fee > 0` — `receipt.success` is not read anywhere in the function [4](#0-3) .

The proof only attests that the *event log was emitted on Ethereum* (i.e., that Ethereum's `Gateway` contract processed the message and emitted the receipt event), not that the underlying command execution succeeded. Since the event itself is designed to carry a `success` flag (implying failed dispatches on Ethereum still emit `InboundMessageDispatched` with `success = false`), the pallet is expected to distinguish between successful and failed deliveries, but the reward code path collapses that distinction — any correctly-proven receipt (success or failure) pays the same fee. This is functionally identical to `getScore` never checking `answerAIsWinner == reactions[...].answer` before paying out based on other fields.

### Impact Explanation
Any relayer whose Ethereum-side dispatch fails (revert, out-of-gas per command, `success = false`) can still submit the (still valid, still Merkle-proven) receipt and be paid the full relayer fee from `PendingOrder::fee`, exactly as if the message had succeeded. This is "public underpriced work" / duplicate-of-intent payout: relayers are financially incentivized to submit receipts regardless of dispatch outcome, and the protocol has no economic penalty for failed dispatches, silently draining the bridge's fee/reward pot to relayers who did not deliver useful work — a reward-conservation violation matching the "Balances ... bridge rewards ... must conserve value and settle exactly once to the rightful beneficiary and amount" pivot.

### Likelihood Explanation
High: `submit_delivery_receipt` is a fully public, unprivileged extrinsic (`ensure_signed(origin)`), and no malicious peer, relayer collusion, or governance action is required — only a correctly-proven Merkle/Beefy receipt for the transaction the relayer already submitted to Ethereum, which will exist regardless of dispatch success since the Gateway emits the event unconditionally per the `success` field's presence.

### Recommendation
Check `receipt.success` before paying the reward, e.g. only call `T::RewardPayment::register_reward` when `receipt.success` is `true`; if `false`, either withhold/reduce the fee or route it to a different party (e.g., burn, refund to the original committer) so the payout is conditioned on the correctness of delivery, analogous to fixing `getScore` to check `answerAIsWinner == reactions[...].answer` before scoring.

### Proof of Concept
1. A message is enqueued and committed by the outbound-queue-v2 pallet, creating a `PendingOrder { nonce, fee, .. }` [5](#0-4) .
2. A relayer submits the message to Ethereum's Gateway contract; execution of the commands reverts/fails, but the Gateway still emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. The relayer builds an `EventProof` for this log and calls `submit_delivery_receipt`. `T::Verifier::verify` succeeds (it only verifies the log was included/finalized on Ethereum, not the dispatch outcome).
4. `DeliveryReceipt::try_from` decodes `success = false` correctly into the receipt struct.
5. `process_delivery_receipt` checks gateway address and pending-order existence only, then unconditionally pays `order.fee` via `T::RewardPayment::register_reward`, exactly as shown in the existing passing test `submit_delivery_receipt_works`-style flow with `success: true` [6](#0-5)  — the same code path executes identically if `success` were `false`, since it is never inspected.

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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L426-443)
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

			Ok(true)
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
