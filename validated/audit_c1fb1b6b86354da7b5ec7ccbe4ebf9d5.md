This is a solid local analog of the "ignore failure status" bug class.

### Title
`process_delivery_receipt` ignores the Ethereum `success` flag and pays relayer reward / settles pending order regardless of delivery outcome - ([File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs])

### Summary
The Snowbridge outbound-queue-v2 pallet's `submit_delivery_receipt` extrinsic decodes a `DeliveryReceipt` from an Ethereum event log that explicitly carries a `success: bool` field describing whether the message dispatch on Ethereum succeeded or reverted, but `Pallet::process_delivery_receipt` never reads or checks this field before paying the relayer reward and deleting the `PendingOrder`. This is a direct analog of the CToken `mint`/`redeemUnderlying` bug class: a call returns an explicit status/error code that callers must check, but the caller unconditionally treats the call as successful.

### Finding Description
`DeliveryReceipt` is decoded from the Ethereum `InboundMessageDispatched(uint64 indexed nonce, bytes32 topic, bool success, bytes32 reward_address)` event log: [1](#0-0) 

The `success` field is populated on decode: [2](#0-1) 

However, `Pallet::process_delivery_receipt`, which is reached through `submit_delivery_receipt` after proof verification, only validates `gateway`, resolves `reward_address`, fetches the `PendingOrder` by `nonce`, pays out the fee as a relayer reward, and removes the order — it never inspects `receipt.success`: [3](#0-2) 

The public entrypoint that drives this is unprivileged (any signed relayer with a valid Merkle/verifier proof for the log can call it): [4](#0-3) 

`T::Verifier::verify` only authenticates that the log genuinely originates from the Gateway contract on Ethereum and was included in a finalized block — it says nothing about the semantic outcome (`success`) encoded inside that log. So a legitimately verified event with `success == false` (i.e. the command execution on Ethereum reverted/failed) is processed identically to `success == true`: the relayer is still rewarded via `T::RewardPayment::register_reward`, and the `PendingOrder` entry is unconditionally removed from `PendingOrders`.

### Impact Explanation
- Reward is paid out for a message that Ethereum itself reports as failed execution — an unbacked/incorrect payout to the relayer (theft-of-value class, since the reward is meant to compensate for genuinely successful delivery).
- The `PendingOrder` record (which tracks the fee owed and is the only state associating a nonce with its outstanding relay task) is deleted regardless of success, permanently discarding the ability to track or retry a failed dispatch and effectively finalizing a delivery/settlement state that never should have been considered final. This matches the "duplicate settlement or payout" / "permanent bridge-state lock" impact categories in the assessment scope, since a failed message can no longer be re-settled or reconciled once its `PendingOrder` is gone.
- No governance, malicious relayer, or malicious node capability is required — a normal, honest relayer submitting a real (but `success = false`) event log triggers this unconditionally; it is a pure runtime logic defect, not an infra/social issue.

### Likelihood Explanation
Any command dispatched through `snowbridge-pallet-outbound-queue-v2` (e.g. token transfers via Gateway commands) can legitimately fail on the Ethereum side for reasons unrelated to the relayer's honesty — insufficient gas allotted by `GasMeter`, a reverting downstream call, agent/channel state issues, etc. Every such failure emits `InboundMessageDispatched(..., success=false, ...)`, and the relayer (who has every incentive to submit the proof to claim payment) can call `submit_delivery_receipt` with that log through the completely public, unprivileged extrinsic. There is no additional gate — verification only checks proof authenticity, not the boolean payload — so the vulnerable path is trivially and repeatedly reachable in normal operation.

### Recommendation
In `Pallet::process_delivery_receipt`, branch on `receipt.success`:
- If `true`, keep current behavior (pay reward, remove/settle order as delivered).
- If `false`, do not pay the relayer reward for a failed dispatch (or pay a reduced/base fee only), and instead of blindly deleting the `PendingOrder`, transition it into a distinct failure/retry state (or emit a `MessageDispatchFailed`/similar event) so that the fee/order bookkeeping accurately reflects that the underlying command was not executed successfully, mirroring the report's recommendation to validate every return/status code and revert (or appropriately branch) instead of silently treating all outcomes as `NO_ERROR`.

### Proof of Concept
1. A message with a non-zero `fee` is enqueued and processed via `do_process_message`, creating a `PendingOrder { nonce, fee, .. }` in storage. [5](#0-4) 
2. The corresponding command is relayed to Ethereum and dispatched by the Gateway contract, but execution reverts/fails (e.g., due to insufficient gas budgeted by `maximum_dispatch_gas_used_at_most`, or a downstream revert). The Gateway still emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. The relayer obtains a valid inclusion proof for this genuine event log and calls `submit_delivery_receipt(origin, event)`.
4. `T::Verifier::verify` succeeds (the log is authentic and included), `DeliveryReceipt::try_from` decodes `success = false` correctly.
5. `process_delivery_receipt` proceeds to call `T::RewardPayment::register_reward(&reward_account, .., order.fee)` and removes `PendingOrders::<T>::remove(nonce)` — exactly as it would for a successful delivery — despite the on-chain fact that the message dispatch failed.
6. Observable result: `Event::MessageDelivered { nonce }` is emitted and the relayer is rewarded, even though the message never executed successfully on Ethereum, and the order state that could have tracked the failure is gone permanently. [6](#0-5)

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
