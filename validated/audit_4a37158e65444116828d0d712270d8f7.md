Confirmed: `receipt.success` is decoded but never referenced anywhere else in the codebase outside its own struct definition, confirming the payout logic in `process_delivery_receipt` never checks it.

Audit Report

## Title
Snowbridge outbound-queue-v2 pays relayer reward regardless of delivery success (`DeliveryReceipt::success` never checked) - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

## Summary
`Pallet::process_delivery_receipt` decodes a `DeliveryReceipt` whose `success: bool` field explicitly encodes whether the Ethereum-side dispatch of the message succeeded, but the payout logic only checks `T::GatewayAddress::get() == receipt.gateway` and `order.fee > 0` before calling `T::RewardPayment::register_reward` [1](#0-0) . The `success` field is decoded from the Ethereum `InboundMessageDispatched` event log [2](#0-1)  but is never read anywhere in the pallet.

## Finding Description
`submit_delivery_receipt` is a public, unprivileged extrinsic reachable via `ensure_signed(origin)` [3](#0-2) . It verifies only that the event log was included/finalized on Ethereum via `T::Verifier::verify`, decodes the log into a `DeliveryReceipt` via `TryFrom<&Log>` — which faithfully copies `event.success` into `receipt.success` [4](#0-3)  — and then calls `process_delivery_receipt`. That function checks the gateway address and pending-order existence, then pays `order.fee` to the reward account whenever `order.fee > 0`, without ever inspecting `receipt.success` [5](#0-4) . A repository-wide search confirms `success` is referenced only in its own struct definition and nowhere in the payout path [6](#0-5) . Since the proof only attests the event log was emitted/finalized on Ethereum — not that the dispatch outcome was successful — and the event schema itself carries a `success` flag (implying failed dispatches also emit this event with `success = false`), the pallet fails to condition payout on dispatch correctness.

## Impact Explanation
Because the reward is paid unconditionally on `order.fee > 0` regardless of `receipt.success`, a relayer whose Ethereum-side dispatch fails can still submit the (still validly Merkle/Beefy-proven) receipt and receive the full relayer fee from `PendingOrder::fee`, exactly as if delivery succeeded. This is a duplicate/underpriced-work-style payout defect: the bridge's fee/reward pot is drained to relayers regardless of whether useful delivery work occurred, violating the invariant that "bridge rewards ... must conserve value and settle exactly once to the rightful beneficiary and amount," since the amount is not correctness-conditioned.

## Likelihood Explanation
High. `submit_delivery_receipt` requires only `ensure_signed(origin)` — any relayer that already submitted a message to the Ethereum Gateway can obtain a Merkle/Beefy proof for the resulting `InboundMessageDispatched` event regardless of its `success` value, and submit it to this pallet with no additional privilege or collusion required [7](#0-6) .

## Recommendation
Condition the reward payout on `receipt.success`, e.g., only call `T::RewardPayment::register_reward` when `receipt.success == true`; for `false`, withhold or reduce the fee, or route it elsewhere (burn/refund), so payout reflects actual delivery outcome rather than only proof-of-log-inclusion.

## Proof of Concept
1. A message is enqueued via `do_process_message`, creating a `PendingOrder { nonce, fee, .. }` [8](#0-7) .
2. A relayer submits the message to Ethereum's Gateway; the dispatch of the underlying commands fails, but the Gateway still emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. The relayer builds an `EventProof` for this log and calls `submit_delivery_receipt`; `T::Verifier::verify` succeeds since it only checks log inclusion/finality, not dispatch outcome [9](#0-8) .
4. `DeliveryReceipt::try_from` decodes `success = false` correctly.
5. `process_delivery_receipt` pays `order.fee` unconditionally, identical to the existing test pattern that uses `success: true` [10](#0-9)  — the same code path executes identically with `success: false` since that field is never inspected.

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
