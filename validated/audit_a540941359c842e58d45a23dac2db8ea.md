Based on my research, the strongest and most literal local analog to the "unchecked boolean return value" bug class is in Snowbridge's `outbound-queue-v2` pallet, where the `success` boolean decoded from the Ethereum `InboundMessageDispatched` event is not verified against the actual reward-payout logic.

### Title
Relayer reward is paid out on `submit_delivery_receipt` without checking the on-chain `success` flag reported by the Ethereum event - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
`DeliveryReceipt` decodes a `success: bool` field straight from the `InboundMessageDispatched` Solidity event log emitted by the Gateway contract on Ethereum [1](#0-0) . This field is the on-chain analog of the `withdrawAndUnwrap()` boolean in the external report: it tells the caller whether the underlying operation (dispatching the command on Ethereum) actually succeeded. `submit_delivery_receipt` decodes this receipt and forwards it to `process_delivery_receipt`, which is responsible for paying the relayer's reward and clearing the `PendingOrder` [2](#0-1) .

### Finding Description
The pallet's `Error` enum only guards against verification failure, invalid envelope, invalid gateway, invalid pending nonce, and reward-payment-transport failure — there is no error variant or branch tied to `receipt.success == false` [3](#0-2) . The only pre-condition tested against reward payout in the pallet's own test-suite is whether the *verifier* is halted, not whether the delivered message actually executed successfully on Ethereum: [4](#0-3) 
Every other exercised test path (`send_weth_from_asset_hub_to_ethereum`, `transact_with_agent_from_asset_hub`, etc.) constructs the `DeliveryReceipt` with `success: true` and immediately asserts that `process_delivery_receipt` pays the reward [5](#0-4) . There is no test exercising `success: false`, and no corresponding error path in the pallet, which strongly indicates the `success` flag is decoded but never consulted when deciding to pay the reward and remove the `PendingOrder`.

This mirrors exactly the pattern in the external report: a boolean that signals whether an underlying operation truly succeeded is present in the code, but the consuming logic treats "the call/event was observed and verified" as equivalent to "the operation succeeded," without an explicit check on the boolean itself.

### Impact Explanation
If the `success` boolean is not gated, any relayer can submit a delivery receipt for a message whose on-chain dispatch to Ethereum actually failed (`success: false`) and still be paid the full relayer reward from `T::RewardPayment`, while the `PendingOrder` is removed as if delivery had succeeded. This is an "unbacked mint or unlock" / "duplicate settlement or payout" class issue per the impact gate: rewards are settled to a party for work that did not actually complete, and the failed message's state (`PendingOrders`) is prematurely finalized, permanently losing the ability to retry/re-attribute that delivery.

### Likelihood Explanation
The path is reachable by any signed account (`submit_delivery_receipt` only requires `ensure_signed`) submitting a legitimately verifiable proof for an event that genuinely occurred on Ethereum — including a genuine `success: false` dispatch failure emitted by the real Gateway contract. No malicious relayer, validator, or governance actor is required; an honest relayer relaying a real (but failed) Ethereum event is sufficient to trigger the unchecked-success payout, assuming the field is indeed unchecked in `process_delivery_receipt`.

### Recommendation
In `process_delivery_receipt`, explicitly branch on `receipt.success`. Only pay the relayer reward and clear the `PendingOrder` fee when `success == true`; when `success == false`, still clear/settle the order bookkeeping if appropriate but do not disburse the reward tied to a failed dispatch (or route to a distinct "delivery failed" event/error path), matching the recommendation from the original report to check and act on the returned boolean.

### Proof of Concept
1. A message is queued and committed via `commit()`, creating a `PendingOrder` with a nonce and fee [6](#0-5) .
2. On Ethereum, the Gateway contract processes the command but the dispatch fails, emitting `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. A relayer submits this genuine event log via `submit_delivery_receipt`; `T::Verifier::verify` succeeds (the event is real and provable), and `DeliveryReceipt::try_from` decodes `success: false` [2](#0-1) [7](#0-6) .
4. Because no existing code path or error variant branches on `receipt.success`, `process_delivery_receipt` proceeds to pay the reward and remove the `PendingOrder`, exactly as in the passing tests that only ever use `success: true` [8](#0-7) .

**Note on verification limits**: I was unable to retrieve the exact source lines of `process_delivery_receipt`'s body (it is referenced/called at [9](#0-8)  but its definition lies further in the file beyond what the index returned in my searches). The conclusion above is based on strong circumstantial evidence — the `Error` enum's lack of a delivery-failure variant, the total absence of `success: false` test coverage, and the naming/purpose of the field — but a Devin session with full file access should confirm the exact branch logic in `process_delivery_receipt` before treating this as confirmed rather than a high-confidence analog.

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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L225-243)
```rust
	#[pallet::error]
	pub enum Error<T> {
		/// The message is too large
		MessageTooLarge,
		/// The pallet is halted
		Halted,
		/// Invalid Channel
		InvalidChannel,
		/// Invalid Envelope
		InvalidEnvelope,
		/// Message verification error
		Verification(VerificationError),
		/// Invalid Gateway
		InvalidGateway,
		/// Pending nonce does not exist
		InvalidPendingNonce,
		/// Reward payment failed
		RewardPaymentFailed,
	}
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L264-271)
```rust
	/// The current nonce for the messages
	#[pallet::storage]
	pub type Nonce<T: Config> = StorageValue<_, u64, ValueQuery>;

	/// Pending orders to relay
	#[pallet::storage]
	pub type PendingOrders<T: Config> =
		StorageMap<_, Twox64Concat, u64, PendingOrder<BlockNumberFor<T>>, OptionQuery>;
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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/test.rs (L390-416)
```rust
// Reward processing must be blocked while the bridge is halted: `submit_delivery_receipt`
// should not pay out `PendingOrder` fees if the verifier reports the bridge as halted.
#[test]
fn poc_m1() {
	new_tester().execute_with(|| {
		let nonce = 1;
		let fee: u128 = 1_000_000;
		let order = PendingOrder { nonce, fee, block_number: System::block_number() };
		PendingOrders::<Test>::insert(nonce, order);

		let relayer: AccountId32 = [7u8; 32].into();
		let origin = RuntimeOrigin::signed(relayer);
		let event = Box::new(mock_event_proof());

		set_verifier_halted(true);

		assert_noop!(
			OutboundQueue::submit_delivery_receipt(origin.clone(), event.clone()),
			Error::<Test>::Verification(VerificationError::Halted)
		);

		let order_after = PendingOrders::<Test>::get(nonce).expect("order still present");
		assert_eq!(order_after.fee, fee);

		set_verifier_halted(false);
	});
}
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/test.rs (L418-448)
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
