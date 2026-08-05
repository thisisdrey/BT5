Confirmed finding: `process_delivery_receipt` in `snowbridge-pallet-outbound-queue-v2` decodes a `DeliveryReceipt` from the `InboundMessageDispatched` Ethereum event log, which includes a `success: bool` field, but the pallet never inspects `receipt.success` before paying out the relayer reward and removing the `PendingOrder`.

### Title
Relayer reward paid and order settled regardless of on-chain message execution outcome (`success` field ignored) - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
The Ethereum Gateway emits `InboundMessageDispatched(nonce, topic, bool success, reward_address)` when it processes a relayed outbound message, signaling whether the dispatched command actually succeeded on Ethereum. `DeliveryReceipt::try_from` decodes this `success` field [1](#0-0) , but `Pallet::process_delivery_receipt`, invoked from the public, unpermissioned `submit_delivery_receipt` extrinsic, only checks `receipt.gateway` and looks up `PendingOrders` by `receipt.nonce` — it never reads `receipt.success` before paying the reward and clearing the order [2](#0-1) .

### Finding Description
`submit_delivery_receipt` is callable by any signed account and only requires a valid Merkle-proved Ethereum event log matching the configured `GatewayAddress` [3](#0-2) . This is a legitimate, non-privileged proof-of-execution log: the Gateway contract emits it whenever it *attempts* to dispatch the command, setting `success=false` on execution failure (reverted command). Because `process_delivery_receipt` ignores that boolean and unconditionally pays `order.fee` to `reward_account` and deletes the `PendingOrder` for that nonce [4](#0-3) , a relayer is rewarded and the pending order is permanently settled even when the outbound command failed on Ethereum. This breaks the "queue/marker only advances after execution and settlement succeed atomically" invariant: the receipt only proves that the Gateway *processed* the log entry, not that the command's effects (e.g., asset unlock/mint on Ethereum) actually took effect.

### Impact Explanation
Once `PendingOrders::remove(nonce)` executes, there is no retry path documented in this pallet for a failed command — the order is gone and the relayer has already been paid from `order.fee`, which is charged against the bridge fee, not from newly minted funds, so this is not a direct fund-theft primitive by itself, but it does allow: (1) relayers to be rewarded for delivering messages whose Ethereum-side effects failed, silently misallocating protocol reward funds, and (2) permanent loss of the retry/tracking state for a failed cross-chain command, since the nonce's `PendingOrder` is deleted unconditionally regardless of `success`. No mechanism in this file exists to resubmit or reprocess a failed nonce once the order is removed.

### Likelihood Explanation
Likelihood is moderate: it requires an actual Gateway-emitted `success=false` event (i.e., a real command execution failure on Ethereum, e.g. reverted XCM transact or an ERC-20 transfer failure inside the dispatched command) — this is not attacker-controlled data forgery, since the proof must correspond to a genuine event log verified by `T::Verifier`. However, no malicious/relayer/admin assumption is required: any legitimate relayer submitting a legitimate failure receipt triggers the unconditional payout/removal path, so this is a logic bug reachable through ordinary operation, not an attack requiring privileged access.

### Recommendation
Branch on `receipt.success` in `process_delivery_receipt`: only pay the reward and remove the `PendingOrder` when `success == true`; on `false`, emit a distinct event (e.g., `MessageDeliveryFailed`) and decide the retry/refund policy (e.g., keep the order for retry, or refund the fee to the original sender) instead of silently rewarding and dropping the order.

### Proof of Concept
1. A message is queued via `do_process_message`, creating `PendingOrders[nonce] = { fee, block_number }` [5](#0-4) .
2. The relayer delivers the message to the Ethereum Gateway, but the dispatched command reverts/fails there; the Gateway still emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. The relayer builds a Merkle proof for this real log and calls `submit_delivery_receipt(event)`.
4. `T::Verifier::verify` succeeds (the log is genuine), `DeliveryReceipt::try_from` decodes `success=false` correctly, but `process_delivery_receipt` never reads it [6](#0-5) .
5. Reward is paid via `T::RewardPayment::register_reward`, and `PendingOrders::remove(nonce)` deletes the order, even though the command failed on Ethereum — confirmed by existing tests that only assert on `success` being ignored, e.g. `submit_delivery_receipt_succeeds_after_unhalt` and the reward/outbound integration tests never construct or assert behavior for `success: false` receipts [7](#0-6) .

**Uncertainty note**: I could not fully trace how a genuine `success=false` event would be handled downstream on the Ethereum Gateway contract side (out of this repo's scope) or whether any off-chain relayer software layer filters out failed deliveries before submitting a receipt — that mitigation, if any, would be external to this repository and not enforced on-chain. This analysis is based solely on the on-chain pallet logic in `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs` and the receipt decoding in `bridges/snowbridge/primitives/outbound-queue/src/v2/delivery_receipt.rs`.

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
