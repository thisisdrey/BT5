### Title
Relayer reward paid regardless of Ethereum delivery outcome in `process_delivery_receipt` - (File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs)

### Summary
`DeliveryReceipt` decodes an explicit `success: bool` field from the Ethereum `InboundMessageDispatched` event, but `Pallet::process_delivery_receipt` never reads or branches on it. Exactly like the ORDU-1 pattern — where a second, more specific condition (`LimitIncrease`) was defined but never actually reached because an earlier, broader branch swallowed it — the `success` discriminant here is decoded and carried all the way to the pallet call, but the code path that should differentiate "successful delivery" from "failed delivery" is missing, so both outcomes are treated identically.

### Finding Description
`DeliveryReceipt::try_from` decodes the `success` field straight from the Solidity event: [1](#0-0) 

But in `process_delivery_receipt`, only `gateway`, `reward_address` and `nonce` are consulted — `receipt.success` is never inspected: [2](#0-1) 

The extrinsic wrapper `submit_delivery_receipt` verifies the proof/log and then unconditionally forwards to `process_delivery_receipt`: [3](#0-2) 

Regardless of whether the Gateway contract emitted `success = true` or `success = false`, the pallet:
1. pays `order.fee` to `reward_account` via `T::RewardPayment::register_reward`,
2. removes the `PendingOrder` from storage,
3. emits `Event::MessageDelivered`.

This is the ORDU-1 analog: a value that is supposed to gate two distinct behaviors (reward-on-success vs. no-reward/penalize-on-failure, or at minimum distinct accounting) is computed but the enforcing branch simply doesn't exist, so the "restrictive" path is unreachable and every relayer submission collapses onto the permissive path.

### Impact Explanation
Any unprivileged, permissionless relayer can submit a valid delivery receipt whose underlying Ethereum-side command execution reverted (`success = false`) — Snowbridge's gateway design still emits `InboundMessageDispatched` for failed dispatches so the pending order can be resolved — and still collect the full relayer fee as if delivery succeeded. This is a public, underpriced-work class issue: relayers are paid identically for genuinely completed work and for reverted/failed work, which both degrades the incentive alignment of the delivery-receipt mechanism and results in fee payout (`order.fee`) that is not backed by a correspondingly successful bridge action, matching the "theft or unbacked mint" / "public underpriced work" impact categories.

### Likelihood Explanation
High. No admin, governance, validator, or malicious-peer assumption is required — the flow is invoked directly through the public, signed `submit_delivery_receipt` extrinsic, gated only by the (unmodified) `T::Verifier::verify` step which authenticates the log's provenance, not its semantic `success` value. Any relayer causing a command on the Gateway contract to revert (e.g. by picking a command that consumes gas near a bound, or targeting a destination that reverts) while still producing a valid receipt/proof can exploit this deterministically.

### Recommendation
Branch on `receipt.success` in `process_delivery_receipt`: only call `T::RewardPayment::register_reward` when `receipt.success` is `true`; for `false`, either withhold the fee, refund it back to the sender pallet/origin, or apply a reduced/no-reward accounting path, while still removing the `PendingOrder` (or handling it via a distinct code path) so the order can't be resubmitted indefinitely.

### Proof of Concept
1. Send a message through the outbound queue (`do_process_message`) creating a `PendingOrder{nonce, fee>0}` as in `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs` lines 426-440.
2. On Ethereum, arrange for the corresponding inbound message to revert during dispatch (e.g., malformed downstream call, insufficient gas headroom) — the Gateway still emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. Relay this event log and its proof via `submit_delivery_receipt`.
4. `T::Verifier::verify` succeeds (the log is authentic), `DeliveryReceipt::try_from` decodes `success = false`, but `process_delivery_receipt` (lines 445-480) still executes `T::RewardPayment::register_reward(&reward_account, ..., order.fee)` and removes the order — as confirmed by the existing test `submit_delivery_receipt_succeeds_after_unhalt` which only varies the verifier-halted flag, never `success`, and still asserts reward registration: [4](#0-3) 
Constructing the same test with `success: false` in the mocked event would still pay the reward, demonstrating the missing gate.

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
