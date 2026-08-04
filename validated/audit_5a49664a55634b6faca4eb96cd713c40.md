## Title
`process_delivery_receipt` pays relayer reward and settles pending order regardless of on-chain execution outcome (`success` field never checked) - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

## Summary
`DeliveryReceipt` decoded from the Ethereum `InboundMessageDispatched` event log carries a `success: bool` field indicating whether the message actually executed successfully on Ethereum [1](#0-0) . However, `Pallet::process_delivery_receipt` never reads or checks this field: it unconditionally pays the relayer reward from `order.fee` and removes the `PendingOrder`, treating the receipt as final settlement regardless of whether the underlying message actually succeeded [2](#0-1) . This mirrors the Governor Bravo bug class: a completion/settlement flag (here, order removal + reward payout) is set based on the wrong signal — the mere existence and gateway-authenticity of a delivery receipt — instead of the actual success outcome that the protocol design intends to gate it on.

## Finding Description
The doc comment for the pallet explicitly states the intended flow: "When the message has been verified **and executed**, the relayer will call the extrinsic `submit_delivery_receipt`" to "fetch the pending order ... pay reward ... remove the order" [3](#0-2) . This confirms the design intent is that reward payout and order settlement should correspond to actual successful execution on Ethereum, which is exactly what the `success` field of `DeliveryReceipt` is meant to convey.

In `process_delivery_receipt`, the guards only check:
1. `receipt.gateway == T::GatewayAddress::get()` (authenticity of gateway address) [4](#0-3) 
2. That `PendingOrders::<T>::get(nonce)` exists [5](#0-4) 

Then it unconditionally pays `order.fee` to the reward account and removes the order: [6](#0-5) 

The `receipt.success` field, decoded correctly from the verified log at `DeliveryReceipt::try_from` [7](#0-6) , is simply discarded — it is never referenced in `process_delivery_receipt`. All the integration tests in the codebase construct `DeliveryReceipt`/mock events with `success: true` and never exercise a `success: false` path against `process_delivery_receipt`'s settlement logic [8](#0-7) , which is consistent with the code path having no branch on `success` to test in the first place.

Existing guards (`Verifier::verify`, gateway-address equality, `PendingOrders` existence) only establish that *a* genuine event log was emitted by the known Gateway contract for that nonce — they do not establish that the dispatched message actually executed successfully on Ethereum. Because `success` is decoded but never consulted, a message that reverted/failed on Ethereum still results in full reward payout and permanent removal of the `PendingOrder` on BridgeHub, exactly as if it had succeeded — analogous to the Governor Bravo bug where `executed` was set `true` prematurely and no longer reflected true completion state.

## Impact Explanation
This is a public-entrypoint (`submit_delivery_receipt`, callable by any signed relayer) accounting bug that causes duplicate/incorrect settlement of value: relayer rewards (`order.fee`) are paid unconditionally, and `PendingOrders` state — the sole record used to prevent double-claiming — is deleted regardless of actual execution success. This falls under "theft or unbacked mint... duplicate settlement or payout" and "message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically" from the impact gate. An unprivileged relayer benefits from rewards for messages that failed to execute on Ethereum, and legitimate retry/compensation flows for failed messages become impossible since the `PendingOrder` is removed on the first (successful-or-not) receipt.

## Likelihood Explanation
Likelihood is high given the field is fully decoded and available (`success: bool` from the Ethereum event), and any relayer can submit any genuine `InboundMessageDispatched` log — including ones reporting `success: false` — for a real dispatched message and still trigger full reward payout and order removal, since no code path distinguishes the two outcomes. No malicious peer, admin, or governance actor is required; a normal relayer submitting a legitimately verified but failed-delivery receipt triggers the incorrect settlement.

## Recommendation
In `process_delivery_receipt`, branch on `receipt.success`:
- If `true`, proceed with the existing reward payment and `PendingOrders::remove`.
- If `false`, do not pay the reward (or pay a reduced/no incentive), and either remove the order with a distinct "failed" event/state transition, or retain it for governance/retry handling, so that fund flow and order settlement match actual on-chain execution outcome as the module documentation intends.

## Proof of Concept
1. A message is queued via `do_process_message`, creating `PendingOrders::<T>::insert(nonce, order)` with `order.fee > 0` [9](#0-8) .
2. On Ethereum, the message dispatch fails, emitting `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. A relayer submits this genuine event log via `submit_delivery_receipt` [10](#0-9) . `T::Verifier::verify` succeeds (it is a real, correctly-proven log), and `DeliveryReceipt::try_from` decodes `success: false` correctly.
4. `process_delivery_receipt` checks gateway match and pending-order existence only, then unconditionally executes `T::RewardPayment::register_reward(...)` and `PendingOrders::<T>::remove(nonce)` [6](#0-5)  — the relayer is rewarded and the order is permanently cleared even though the message failed on Ethereum, matching the `poc_m1`/`submit_delivery_receipt_succeeds_after_unhalt` test pattern but with `success: false` instead of the halted-verifier case that is actually tested [11](#0-10) .

### Citations

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/delivery_receipt.rs (L14-27)
```rust
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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L36-41)
```rust
//! 10. When the message has been verified and executed, the relayer will call the extrinsic
//!     `submit_delivery_receipt` to:
//! 	a. Verify the message with proof for a transaction receipt containing the event log,
//! 	   same as the inbound queue verification flow
//! 	b. Fetch the pending order by nonce of the message, pay reward with fee attached in the order
//!    	c. Remove the order from `PendingOrders` map storage by nonce
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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/test.rs (L420-449)
```rust
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
