Audit Report

## Title
Outbound bridge messages that never receive a delivery receipt permanently lock the prepaid relayer fee with no cancellation or refund mechanism - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

## Summary
The Snowbridge V2 outbound queue records a `PendingOrder{nonce, fee, block_number}` for every outbound message and pays the recorded `fee` to a relayer only via `process_delivery_receipt`, which requires a valid Ethereum-side `DeliveryReceipt`. No other code path in the pallet ever reads, expires, or refunds a `PendingOrders` entry, so if no valid receipt is ever produced for a nonce (permanent Ethereum-side revert, halted/paused bridge, or no relayer finding it profitable to submit), the associated fee is permanently stranded in storage with no way to reclaim it.

## Finding Description
`do_process_message` in `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs` inserts a `PendingOrder` keyed by `nonce` containing the prepaid `fee` and the `block_number` at insertion time [1](#0-0) . The `block_number` field is defined on `PendingOrder` [2](#0-1)  but it is never compared to the current block anywhere in the pallet to trigger an expiry — a `grep` across the codebase shows `PendingOrders` is only touched by `do_process_message` (insert), `process_delivery_receipt` (remove/pay), `add_tip` (mutate fee), and tests. The only extrinsic capable of removing an entry and settling the fee is `submit_delivery_receipt` → `process_delivery_receipt`, which requires `T::Verifier::verify` to succeed against a genuine Ethereum event log and proof [3](#0-2) . There is no `on_idle`/`on_initialize` sweep, no timeout extrinsic, and no refund-to-sender path anywhere in the pallet, `system-v2` pallet, or the wider bridge-hub runtime code that was inspected. Once the verifier is permanently halted, the Ethereum command permanently reverts, or no relayer submits the receipt (e.g., because it is unprofitable), the `fee` locked in the order can never be released back to the sender nor paid to any relayer.

## Impact Explanation
Every fee-bearing V2 message (asset transfers, `Transact` calls routed through `snowbridge_pallet_system_v2::Pallet::send` or the XCM V2 exporter) that never receives a delivery receipt results in permanent loss of the prepaid fee for the originating account, with no code path to reclaim it. This matches the "permanent user-fund or bridge-state lock" category explicitly accepted by the Polkadot SDK impact gate for Snowbridge/BridgeHub code.

## Likelihood Explanation
No attacker action is required to trigger the lock — an Ethereum-side revert of the dispatched command, a Gateway operating-mode halt of any duration exceeding relayer patience, or simply no relayer finding the fee profitable to claim are all ordinary, non-malicious conditions. The pallet's own test `submit_delivery_receipt_succeeds_after_unhalt` confirms the order remains untouched in `PendingOrders` while halted [4](#0-3) , and nothing in the codebase provides a bound on how long that condition, or a permanent Ethereum-side failure, can persist.

## Recommendation
Add a cancellation/refund mechanism for `PendingOrders`, e.g., a `cancel_stale_order(nonce)` extrinsic or `on_idle` sweep, callable once `current_block - order.block_number` exceeds a configurable threshold, which refunds `order.fee` to the original sender/origin and removes the entry from `PendingOrders`. Ensure this refund path is reachable even while the verifier/bridge remains halted for an extended period.

## Proof of Concept
1. `snowbridge_pallet_system_v2::Pallet::send` (or the V2 XCM exporter) sends a message with a nonzero `fee`.
2. `do_process_message` assigns a `nonce` and inserts `PendingOrder{nonce, fee, block_number}` into `PendingOrders` (`bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs:426-436`).
3. As shown in `submit_delivery_receipt_succeeds_after_unhalt`, while the verifier is halted, `submit_delivery_receipt` fails with `Error::Verification(VerificationError::Halted)` and the order remains in storage untouched.
4. If the halt persists indefinitely, or the Ethereum-side command permanently reverts so no valid `DeliveryReceipt` will ever exist for that nonce, `PendingOrders[nonce]` remains in storage forever, and the fee debited from the sender at step 1 is never returned or paid out, since no other extrinsic in the pallet can remove or refund that entry.

### Citations

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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/types.rs (L14-24)
```rust
/// Pending order
#[derive(Encode, Decode, TypeInfo, Clone, Eq, PartialEq, Debug, MaxEncodedLen)]
pub struct PendingOrder<BlockNumber> {
	/// The nonce used to identify the message
	pub nonce: u64,
	/// The block number in which the message was committed
	pub block_number: BlockNumber,
	/// The fee in Ether provided by the user to incentivize message delivery
	#[codec(compact)]
	pub fee: u128,
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
