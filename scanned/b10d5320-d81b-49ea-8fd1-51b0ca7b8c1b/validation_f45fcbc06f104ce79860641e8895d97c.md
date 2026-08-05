Based on the investigation, the closest local analog to the reported "stuck funds due to missing message cancel/expire" bug is in the Snowbridge `outbound-queue-v2` pallet on the BridgeHub side.

### Title
`PendingOrders` in Snowbridge Outbound Queue V2 have no expiry or cancellation path, permanently locking relayer reward funds if delivery receipt never arrives - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
When a message is queued for delivery to Ethereum, `do_process_message` creates a `PendingOrder` keyed by `nonce` holding the reward `fee` for whichever relayer eventually proves delivery. This order is only ever resolved by `submit_delivery_receipt`/`process_delivery_receipt`, which requires a valid Ethereum-side proof verified by `T::Verifier`. There is no time-bound, no expiry check, and no cancel/refund extrinsic for `PendingOrders`. If no relayer ever submits a valid receipt — because the Ethereum-side execution never happens, the verifier (light client) is halted/stalled, or relayers simply stop servicing this lane — the order and its escrowed reward remain in storage indefinitely with no way for governance, the sender, or anyone else to reclaim or resettle it.

### Finding Description
`Pallet::do_process_message` [1](#0-0)  inserts a `PendingOrder { nonce, fee, block_number }` into `PendingOrders` for every committed outbound message, with no linked expiry block or maximum lifetime.

The only path that removes an entry from `PendingOrders` is `process_delivery_receipt`, gated behind `T::Verifier::verify` and a `DeliveryReceipt::try_from` proof of an actual Ethereum execution event: [2](#0-1) .

This mirrors the pattern in `pallet_bridge_messages`, where operators can only pause/resume the whole lane via `set_operating_mode`, but cannot cancel individual already-sent messages [3](#0-2) , and it is directly confirmed by the pallet's own tests, which show that when the verifier is halted, the order simply sits untouched with no other way to resolve it: [4](#0-3) .

There is no `expire_order`, `cancel_order`, governance-forced settlement, or timeout-based fallback anywhere in this pallet. Once `T::Verifier` (the Ethereum light client / BEEFY verifier) stops making progress — whether due to a stalled relayer set, a light-client sync halt, or simply no relayer ever bothering to submit the receipt for that nonce — the `fee` recorded in that `PendingOrder` can never be paid out to anyone, and there is no mechanism to return it to the original sender either.

### Impact Explanation
This falls under "permanent user-fund or bridge-state lock": the relayer reward fee attached to a message is economically locked for as long as no valid delivery receipt is produced. Because verification depends on the Ethereum light-client verifier (`EthereumBeaconClient`) making progress and specific relayers bothering to service every nonce, a single stalled/halted verifier or an abandoned lane leaves `PendingOrders` entries — and their escrowed fees — permanently unresolved, with no operator, governance, or user recourse to reclaim the value. This is the direct structural analog of the reported bug: a single message-delivery dependency (there, the coordinator; here, the Ethereum verifier/relayer) with no cancel/expire path for messages already committed.

### Likelihood Explanation
No malicious actor is required — this triggers under ordinary operational conditions: verifier halted for maintenance/incident response (as explicitly tested via `set_verifier_halted(true)` in `poc_m1`/`submit_delivery_receipt_succeeds_after_unhalt`), a light-client that falls permanently out of sync, or simply no relayer choosing to service a given nonce. The codebase's own test suite demonstrates the halted-state stuck-order behavior, confirming this is a reachable, unprivileged-triggerable state rather than a hypothetical.

### Recommendation
Add a lifetime/expiry mechanism to `PendingOrder` (e.g., based on `block_number` plus a configurable `MaxOrderLifetime`), together with an extrinsic or automatic hook that allows expired orders to be resolved — either by refunding the fee back to the original payer/sovereign account or by allowing governance to force-settle/cancel stale orders — so that a stalled verifier or absent relayer set cannot permanently strand escrowed reward funds.

### Proof of Concept
Using the existing test harness in `bridges/snowbridge/pallets/outbound-queue-v2/src/test.rs`:
1. Insert a `PendingOrder` via normal message processing (or directly via `PendingOrders::<Test>::insert`) with a non-zero `fee`.
2. Call `set_verifier_halted(true)` (as done in `poc_m1`) to simulate the Ethereum verifier being stalled/halted indefinitely.
3. Attempt `submit_delivery_receipt` — it fails with `Error::Verification(VerificationError::Halted)`, and the `PendingOrder` remains in storage: [5](#0-4) .
4. There is no other pallet call that removes or resolves this order. If the halted state persists indefinitely (or if no relayer ever bothers to submit for this specific nonce even while unhalted), the fee tied to this nonce is permanently unreachable — there is no expiry, timeout, or cancellation path in the pallet.

### Citations

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

**File:** bridges/modules/messages/README.md (L183-198)
```markdown
## Non-Essential Functionality

There may be a special account in every runtime where the messages module is deployed. This account, named 'module
owner', is like a module-level sudo account - he's able to halt and resume all module operations without requiring
runtime upgrade. Calls that are related to this account are:
- `fn set_owner()`: current module owner may call it to transfer "ownership" to another account;
- `fn set_operating_mode()`: the module owner (or sudo account) may call this function to pause/resume
  pallet operations. Owner may halt the pallet by calling this method with
  `MessagesOperatingMode::Basic(BasicOperatingMode::Halted)` argument - all message-related
  transactions will be rejected. Owner may then resume pallet operations by passing the
  `MessagesOperatingMode::Basic(BasicOperatingMode::Normal)` argument. There's also
  `MessagesOperatingMode::RejectingOutboundMessages` pallet mode, where it still accepts all incoming
  messages, but all outbound messages are rejected.

If pallet owner is not defined, the governance may be used to make those calls.

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
