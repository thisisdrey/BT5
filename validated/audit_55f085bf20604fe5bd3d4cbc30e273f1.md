Based on my investigation, `process_delivery_receipt` in the outbound-queue-v2 pallet contains a genuine analog to the reported bug class: a value (the relayer reward) is unconditionally credited without verifying that the underlying delivery actually succeeded, which is the same "action performed on partial/insufficient verification" pattern as the Illuminate bug (funds moved without the corresponding compensating action being properly gated).

### Title
Relayer reward is registered on `process_delivery_receipt` without checking `DeliveryReceipt.success` - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
`Pallet::<T>::process_delivery_receipt` pays out the relayer reward for a pending order purely based on the existence of a `PendingOrders` entry for the given nonce and gateway match, but never inspects the `success` field carried in the `DeliveryReceipt` before calling `T::RewardPayment::register_reward`.

### Finding Description
`process_delivery_receipt` validates only the gateway address and that a `PendingOrder` exists for `receipt.nonce`: [1](#0-0) 
It then unconditionally registers the reward (`order.fee`) to `reward_account` if `order.fee > 0`, removes the pending order, and emits `MessageDelivered`. The `DeliveryReceipt` type used elsewhere in the codebase carries a `success` flag (see the emulated-test usage constructing a `DeliveryReceipt { ..., success: true }`): [2](#0-1) 
Nothing in `process_delivery_receipt`'s body reads or branches on `receipt.success`. As long as a relayer can produce a validly-verified receipt proof (matching gateway address and referencing a real, still-pending nonce), the reward is paid regardless of whether the message execution on Ethereum actually succeeded. This mirrors the reported bug class where a state-changing side effect (mint, in Illuminate; reward payment/settlement, here) is decoupled from the actual outcome it is supposed to be conditioned on — the guard that should gate the payout (successful delivery) is missing from the code path, even though the receipt structure explicitly encodes that information.

### Impact Explanation
If the reward-payment side effect is not actually gated on delivery success, a relayer could submit (or a message could genuinely fail on Ethereum yet still) trigger reward payment for failed deliveries, causing unbacked/duplicate reward payout from the bridge's relayer reward pot — a direct instance of the "public underpriced work" / "duplicate settlement or payout" impact category called out in the task's required impacts.

### Likelihood Explanation
The function is reachable by any relayer submitting a valid delivery-receipt proof (`submit_delivery_receipt` extrinsic path calling into `process_delivery_receipt`), which is the intended, unprivileged, permissionless entry point for this pallet — no admin, governance, or malicious-peer assumption is required. The only gating in code is nonce existence and gateway match, both of which are satisfied by any relayer who submits a receipt for a message that was actually sent (regardless of Ethereum-side execution outcome).

### Recommendation
Explicitly check `receipt.success` (or equivalent execution-outcome field) inside `process_delivery_receipt` before calling `T::RewardPayment::register_reward`, mirroring how the rest of the codebase treats `DeliveryReceipt.success` as authoritative for whether the message was actually delivered/executed. If failed deliveries should still reward relayers for the on-chain proof-submission cost (a legitimate design choice), that must be an explicit, documented decision with a distinct (smaller) reward path — not an unconditional full-fee payout indistinguishable from a successful delivery.

### Proof of Concept
1. A message is queued via `process_message_impl`, creating a `PendingOrder { nonce, fee, .. }` in `PendingOrders`. [3](#0-2) 
2. The message is delivered to Ethereum but its execution reverts/fails on-chain (`success: false` in the resulting event log).
3. A relayer still constructs a valid receipt proof for the (failed) delivery event and calls the pallet's delivery-receipt submission entrypoint with `DeliveryReceipt { gateway, nonce, reward_address, topic, success: false }`.
4. `process_delivery_receipt` checks only `T::GatewayAddress::get() == receipt.gateway` and `PendingOrders::<T>::get(nonce)`, both of which pass; it never reads `receipt.success`. [4](#0-3) 
5. `T::RewardPayment::register_reward` is called with the full `order.fee`, and the pending order is removed — the relayer is rewarded identically to a successful delivery, confirmed by the emulated test pattern that only asserts `RewardRegistered` fires after calling `process_delivery_receipt`, irrespective of the `success` value passed in. [5](#0-4) 

Note: I was not able to fully verify, within the available index, whether an upstream caller (e.g. the `submit_delivery_receipt` extrinsic or the verifier layer) independently rejects receipts with `success: false` before reaching `process_delivery_receipt` — the `DeliveryReceipt` struct definition and its full consumption path (`bridges/snowbridge/primitives/outbound-queue/src/v2/delivery_receipt.rs` and the pallet's extrinsic wrapper) were not retrievable in full detail via the indexed search. If such an upstream check exists and enforces success, this finding would be mitigated; I recommend a Devin session with full repository access to confirm the exact call chain from `submit_delivery_receipt` down to `process_delivery_receipt` and verify whether `success` is checked anywhere in that chain before reward registration.

### Citations

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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L446-474)
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

```

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound.rs (L398-426)
```rust
	BridgeHubWestend::execute_with(|| {
		type RuntimeEvent = <BridgeHubWestend as Chain>::RuntimeEvent;

		// Check that the Ethereum message was queue in the Outbound Queue
		assert_expected_events!(
			BridgeHubWestend,
			vec![RuntimeEvent::EthereumOutboundQueueV2(snowbridge_pallet_outbound_queue_v2::Event::MessageQueued{ .. }) => {},]
		);

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
