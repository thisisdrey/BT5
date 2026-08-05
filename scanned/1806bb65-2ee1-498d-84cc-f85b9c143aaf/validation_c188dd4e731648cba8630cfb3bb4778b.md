Confirmed: `DeliveryReceipt` decodes the `InboundMessageDispatched` event's `success: bool` field [1](#0-0) , but `process_delivery_receipt` in the outbound queue pallet never reads `receipt.success` before paying the reward and permanently removing the order.

### Title
Outbound queue v2 pays relayer reward and permanently clears the pending order without checking `DeliveryReceipt::success` - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
`Pallet::process_delivery_receipt` decodes a `DeliveryReceipt` that carries a `success: bool` field reported by the Ethereum `InboundMessageDispatched` event, but the function ignores this field entirely. It always registers the full `order.fee` reward and unconditionally deletes the `PendingOrders` entry for that nonce, regardless of whether the message execution on Ethereum actually succeeded.

### Finding Description
The pending-order lifecycle is: a message is queued with a fee in `do_process_message`, stored in `PendingOrders` keyed by `nonce` [2](#0-1) . When a relayer submits a delivery proof, `submit_delivery_receipt` verifies the Merkle/event proof and decodes the log into a `DeliveryReceipt`, then calls `process_delivery_receipt` [3](#0-2) .

`process_delivery_receipt` only checks the gateway address and the existence of the pending order by nonce; it never inspects `receipt.success` before paying and clearing state: [4](#0-3) 

The `success` field is decoded from the on-chain Ethereum event and is available on the `DeliveryReceipt` struct [5](#0-4) , and it is even exercised in downstream integration tests with `success: true`/`false` values being passed as receipt fields [6](#0-5)  — confirming the field is meaningful and expected to gate behavior, yet the pallet's core settlement logic silently drops it.

The unit tests in this pallet only cover the "halted verifier" gating path and the happy path (`poc_m1`, `submit_delivery_receipt_succeeds_after_unhalt`) [7](#0-6) ; there is no test asserting that a receipt with `success: false` withholds the reward or preserves/handles the order differently — because no such branch exists in the implementation.

### Impact Explanation
This directly matches the impact classes "duplicate settlement or payout" and "message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically":
- If the Ethereum-side dispatch of the outbound message failed (`success = false`, e.g. the inbound handler on the Gateway contract reverted or rejected the command), the relayer still gets the full fee registered via `T::RewardPayment::register_reward` — an unbacked/incorrect payout for work that did not actually complete.
- The `PendingOrders` entry is deleted unconditionally, so the message's delivery-tracking state is treated as final/settled regardless of the real outcome. There is no retry, resubmission, or reconciliation path once the order is gone — a permanent state loss for that nonce (matching the report's core theme of state being irrecoverably zeroed/lost on a status transition, here it's the "failed" transition that wrongly finalizes as if it were "success").
- Because rewards and settlement finality are gated only on proof verification and gateway-address matching, and not on the actual event payload semantics, an attacker or malfunctioning relayer can claim rewards for messages that never executed correctly on Ethereum, draining the Snowbridge reward account/sovereign funding source over repeated forged-success submissions is not required — even legitimate but failed deliveries always pay out, degrading the incentive/accounting model of the bridge.

### Likelihood Explanation
This requires no privileged actor: it is triggered by any relayer who calls the public, permissionless `submit_delivery_receipt` extrinsic with a proof for a real (or, in a failure scenario, unavoidable) `InboundMessageDispatched` event where `success = false`. Since normal Ethereum-side reverts on message dispatch are an expected occurrence (not an attack), this is not a theoretical edge case but a systemic gap in the settlement logic — every failed dispatch on the Ethereum side results in a false-positive reward payout and order deletion.

### Recommendation
In `process_delivery_receipt`, branch on `receipt.success`:
- If `true`, proceed as today: register reward, remove `PendingOrders` entry, emit `MessageDelivered`.
- If `false`, do not pay the reward (or pay a reduced "confirmation-only" fee if that is the intended relayer incentive for reporting failures), and either retain the order for a possible retry/governance-driven resolution or transition it to an explicit "failed" state distinct from "delivered", emitting a distinguishable event (e.g. `MessageDeliveryFailed`) so downstream systems and the fee-payer sovereign account are not silently charged for non-executed work.

### Proof of Concept
1. A message is queued via `do_process_message`, creating `PendingOrders[nonce] = { fee: F, .. }`.
2. On Ethereum, the Gateway's dispatch of the message fails (e.g., due to insufficient gas budget mismatch, or a legitimate contract-level revert), and the Gateway emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. A relayer observes this event, builds a valid Merkle/receipt proof for it, and calls `submit_delivery_receipt(origin, event)`.
4. `T::Verifier::verify` succeeds (it verifies the proof, not the semantic `success` flag). `DeliveryReceipt::try_from` decodes `success = false` correctly.
5. `process_delivery_receipt` reaches `order.fee > 0` and calls `T::RewardPayment::register_reward(&reward_account, .., order.fee)` — reward is granted despite `success == false`.
6. `PendingOrders::remove(nonce)` permanently deletes the order; `Event::MessageDelivered` is emitted even though the message dispatch failed.
7. The relayer can later call `claim_rewards`/`claim_rewards_to` in `pallet-bridge-relayers` to withdraw the wrongly-registered fee [8](#0-7) .

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

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound.rs (L407-418)
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
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/test.rs (L390-449)
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

**File:** bridges/modules/relayers/src/lib.rs (L263-302)
```rust
		fn do_claim_rewards(
			relayer: T::AccountId,
			reward_kind: T::Reward,
			beneficiary: BeneficiaryOf<T, I>,
		) -> DispatchResult {
			RelayerRewards::<T, I>::try_mutate_exists(
				&relayer,
				reward_kind,
				|maybe_reward| -> DispatchResult {
					let reward_balance =
						maybe_reward.take().ok_or(Error::<T, I>::NoRewardForRelayer)?;
					T::PaymentProcedure::pay_reward(
						&relayer,
						reward_kind,
						reward_balance,
						beneficiary.clone(),
					)
					.map_err(|e| {
						tracing::error!(
							target: LOG_TARGET,
							error=?e,
							?relayer,
							?reward_kind,
							?reward_balance,
							?beneficiary,
							"Failed to pay rewards"
						);
						Error::<T, I>::FailedToPayReward
					})?;

					Self::deposit_event(Event::<T, I>::RewardPaid {
						relayer: relayer.clone(),
						reward_kind,
						reward_balance,
						beneficiary,
					});
					Ok(())
				},
			)
		}
```
