## Title
`process_delivery_receipt` pays relayer reward and settles the pending order without checking the `DeliveryReceipt.success` flag — (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
In the Snowbridge V2 outbound queue pallet, `Pallet::process_delivery_receipt` decodes a `DeliveryReceipt` from an Ethereum event log and, based solely on the presence of a `PendingOrders` entry for the given `nonce`, pays the relayer's fee and removes the order — permanently marking it as delivered. The `DeliveryReceipt::success` field, which is decoded straight from the Ethereum `InboundMessageDispatched(nonce, topic, success, reward_address)` event and is meant to indicate whether the message actually executed successfully on Ethereum, is never inspected anywhere in the pallet. [1](#0-0) [2](#0-1) 

### Finding Description
This is the same bug class as the external report: a status value that is supposed to gate a payout ("Settled" reached only via legitimate settlement vs. via `abort`) is set/available but the downstream payout function never checks the fine-grained sub-status before releasing funds — it only checks a coarse condition (in the polkadot-sdk case: "does a `PendingOrders` entry exist for this nonce").

`process_delivery_receipt` is called from `submit_delivery_receipt` after the event log is cryptographically verified by `T::Verifier` (proving the log genuinely originated from the Gateway contract on Ethereum), and after `DeliveryReceipt::try_from(&log)` decodes the log fields, including `success`: [3](#0-2) 

The actual settlement logic then does:
```
ensure!(T::GatewayAddress::get() == receipt.gateway, Error::<T>::InvalidGateway);
let reward_account = ...;
let nonce = receipt.nonce;
let order = <PendingOrders<T>>::get(nonce).ok_or(Error::<T>::InvalidPendingNonce)?;
if order.fee > 0 {
    T::RewardPayment::register_reward(&reward_account, T::DefaultRewardKind::get(), order.fee);
}
<PendingOrders<T>>::remove(nonce);
Self::deposit_event(Event::MessageDelivered { nonce });
``` [4](#0-3) 

Nothing in this path (or anywhere in the crate, confirmed by the grep for `receipt.success`/`.success`, which only matches the struct field definition) branches on `receipt.success`. Whether the Ethereum Gateway actually dispatched the message successfully or the dispatch reverted, the same code path executes: reward is paid and the order is deleted with `MessageDelivered` emitted unconditionally.

This mirrors the original report's flaw precisely: the guard used to gate the payout (`offerInfo.offerStatus == Settled` there; "order exists in `PendingOrders`" here) does not distinguish between the legitimate-completion path and an "aborted"/failed path that is representable in the same data structure (`abortOfferStatus` there; `success: bool` here). The finer-grained field exists specifically to make this distinction, but the payout code ignores it.

### Impact Explanation
- Reward funds (Ether-denominated relayer rewards recorded via `T::RewardPayment::register_reward`, later payable through `pallet-bridge-relayers`) are paid to relayers for messages whose on-chain Ethereum execution failed (`success == false`), i.e. the relayer is compensated for work that did not achieve its intended effect.
- The `PendingOrder` is removed regardless of `success`, so `MessageDelivered` is emitted and the message is treated as fully settled even when Ethereum-side execution failed. There is no retry/replay path for a failed delivery once the order is removed — this can permanently lock or misrepresent the delivery state of that message, since the pallet has no other mechanism to re-attempt or flag the failed dispatch once `PendingOrders` no longer holds the nonce.
- Because rewards are paid from a shared/sovereign reward pool (registered via `pallet-bridge-relayers::RewardLedger`), unearned reward payouts here drain funds that would otherwise cover legitimate relayer work — this is a duplicate/incorrect-settlement class of fund loss, consistent with the "Impacts" criteria (duplicate settlement or payout; theft or unbacked mint/unlock of value).
- No malicious peer/validator/relayer collusion is required beyond the relayer simply submitting a truthful proof of an Ethereum event whose `success` field happens to be `false` (e.g., the destination Gateway call reverted due to gas exhaustion or a destination-side revert) — this can occur in entirely honest operation, not just adversarially, but an adversary can also deliberately construct/trigger message content that they know will fail on execution while still collecting the reward.

### Likelihood Explanation
The `success` field is populated straight from the real `InboundMessageDispatched` Ethereum event, so any relayer submitting a genuine, verifier-passing delivery receipt for a message that reverted on Ethereum will trigger this path with no additional effort. No governance, validator, or privileged action is needed — `submit_delivery_receipt` is a plain signed extrinsic open to any account holding the correct proof. The condition ("some destination execution outcome fails") is a normal, expected occurrence in cross-chain messaging (out-of-gas, insufficient allowance/balance on the destination, reentrancy guards, etc.), making this a realistically frequent occurrence rather than a purely theoretical edge case.

### Recommendation
In `process_delivery_receipt`, branch on `receipt.success` before paying the reward and/or before permanently removing the `PendingOrder`:
- If `receipt.success == false`, do not call `T::RewardPayment::register_reward` (or pay a reduced/zero reward), and record the failure explicitly (e.g., a distinct `MessageDeliveryFailed { nonce }` event) rather than emitting `MessageDelivered`.
- Consider retaining a record of failed deliveries (or requiring a separate governance/retry mechanism) instead of unconditionally deleting the `PendingOrders` entry, so failed message dispatches are not silently treated as final successful settlement.

### Proof of Concept
Because this analog was located via static/code-search review rather than by running the pallet's test harness, a full executable PoC could not be produced in this session. The exploit path is straightforward to reconstruct conceptually:
1. A message is sent from the parachain to Ethereum via the outbound queue (`do_process_message`), creating a `PendingOrders` entry with a non-zero `fee`. [5](#0-4) 
2. On Ethereum, the Gateway's dispatch of this message to its final destination reverts (e.g., insufficient gas budget, destination contract logic reverts) — the Gateway emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. A relayer (or the same actor) obtains a valid receipt/execution proof for this real event and calls `submit_delivery_receipt`, which passes `T::Verifier::verify` (the log is genuine) and decodes into a `DeliveryReceipt { success: false, .. }`.
4. `process_delivery_receipt` finds the `PendingOrders` entry for `nonce`, unconditionally calls `T::RewardPayment::register_reward(&reward_account, ..., order.fee)`, removes the order, and emits `MessageDelivered`. [6](#0-5) 
5. The relayer subsequently claims this reward via `pallet-bridge-relayers::claim_rewards`, as demonstrated by the emulated test flow that calls `process_delivery_receipt` directly and observes `RewardRegistered`. [7](#0-6) 

The existing test suite for this pallet only exercises the `Halted`/verifier-failure guard (`poc_m1`, `submit_delivery_receipt_succeeds_after_unhalt`) and the invalid-nonce guard (`invalid_nonce_for_delivery_receipt_fails`); none of the tests set `success: false` in a `DeliveryReceipt` and assert that no reward is paid, corroborating that this check is genuinely absent from both the implementation and its test coverage. [8](#0-7) [9](#0-8)

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

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound.rs (L398-427)
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
}
```

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound.rs (L949-969)
```rust
#[test]
fn invalid_nonce_for_delivery_receipt_fails() {
	BridgeHubWestend::execute_with(|| {
		type Runtime = <BridgeHubWestend as Chain>::Runtime;

		let relayer = BridgeHubWestendSender::get();
		let reward_account = AssetHubWestendReceiver::get();
		let receipt = DeliveryReceipt {
			gateway: EthereumGatewayAddress::get(),
			nonce: 0,
			reward_address: reward_account.into(),
			topic: H256::zero(),
			success: true,
		};

		assert_err!(
			EthereumOutboundQueueV2::process_delivery_receipt(relayer, receipt),
			Error::<Runtime>::InvalidPendingNonce
		);
	});
}
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/test.rs (L418-449)
```rust
// After governance resumes the bridge, legitimate delivery receipts flow through again:
// the order is paid out and removed from storage.
#[test]
fn submit_delivery_receipt_succeeds_after_unhalt() {
	new_tester().execute_with(|| {
		let nonce = 0;
		let fee: u128 = 1_000_000;
		let order = PendingOrder { nonce, fee, block_number: System::block_number() };
		PendingOrders::<Test>::insert(nonce, order);

		let relayer: AccountId32 = [7u8; 32].into();
		let origin = RuntimeOrigin::signed(relayer);
		let event = Box::new(mock_valid_event_proof());

		// Bridge halted — receipt rejected, order untouched.
		set_verifier_halted(true);
		assert_noop!(
			OutboundQueue::submit_delivery_receipt(origin.clone(), event.clone()),
			Error::<Test>::Verification(VerificationError::Halted)
		);
		assert!(PendingOrders::<Test>::get(nonce).is_some());

		// Bridge resumed — same receipt succeeds and the order is settled.
		set_verifier_halted(false);
		assert_ok!(OutboundQueue::submit_delivery_receipt(origin, event));
		assert!(PendingOrders::<Test>::get(nonce).is_none());

		System::assert_has_event(mock::RuntimeEvent::OutboundQueue(Event::MessageDelivered {
			nonce,
		}));
	});
}
```
