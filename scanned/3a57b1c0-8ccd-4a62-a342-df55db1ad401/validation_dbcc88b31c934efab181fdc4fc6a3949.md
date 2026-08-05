### Title
`process_delivery_receipt` pays the full relayer reward without checking the Ethereum-side `success` flag - ([File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs])

### Summary
The external report's broken invariant is: a claim/payout function checks only that a request exists (`distributionEvents[id]`) but never validates the funding-completion flag (`amountPaidIn`) before releasing funds, letting a payout proceed even when the precondition it is supposed to gate was never satisfied. The same class of bug exists in Snowbridge's outbound queue v2 reward flow: `process_delivery_receipt` reads a `DeliveryReceipt` that carries an explicit `success: bool` field decoded straight from the Ethereum `InboundMessageDispatched` event log, but that field is never checked before the relayer reward is registered.

### Finding Description
`DeliveryReceipt` is decoded from an on-chain Ethereum event log and explicitly carries a `success` flag indicating whether the inbound message dispatch actually succeeded on the gateway contract: [1](#0-0) 

`submit_delivery_receipt` is a public, unprivileged extrinsic (`ensure_signed`) that verifies the merkle/header proof for the log, decodes the `DeliveryReceipt`, and forwards it to `process_delivery_receipt`: [2](#0-1) 

`process_delivery_receipt` only checks the gateway address and whether `order.fee > 0`; it never inspects `receipt.success` before calling `T::RewardPayment::register_reward` and removing the pending order: [3](#0-2) 

This is the same pattern as the external report: a boolean completion/precondition flag (`amountPaidIn` in the Solidity report, `success` here) is present in the data model but is never enforced by a `require`/`ensure!` guard on the payout path, so the payout logic behaves as if the precondition were always satisfied.

### Impact Explanation
Because `receipt.success` is ignored, any properly proven `InboundMessageDispatched` log — including ones where `success == false` (dispatch failed on Ethereum, e.g. reverted command, out-of-gas, invalid payload) — still results in:
1. `T::RewardPayment::register_reward` crediting the full `order.fee` to the relayer/reward account, and
2. `PendingOrders::<T>::remove(nonce)` permanently clearing the order, foreclosing any retry/compensation logic that depends on the order still existing.

This is a public, underpriced-work / incorrect-payout condition: a relayer only needs a valid proof of *any* dispatch outcome (not a successful one) to receive full payment, decoupling the reward from actual correct bridge delivery. Depending on how the reward pot and delivery-completion invariants are meant to interact elsewhere in the system, this can drain the reward pot for deliveries that did not actually complete, and it removes state (`PendingOrders`) that downstream logic might rely on to know a message still needs delivery/retry.

### Likelihood Explanation
The path is reachable by any unprivileged, signed account via `submit_delivery_receipt` with only a valid Ethereum event-log proof (which the relayer, as an off-chain third party controlling receipt content only in the sense of *which* log they submit, does not need to influence — they simply need one log to exist, successful or not). No governance, admin, validator, or privileged role is required; this is a normal relayer-facing extrinsic in the intended reward flow, matching the "public dispatch wrapper" / "duplicate or wrong-amount settlement" pivot category.

### Recommendation
Add an explicit check on `receipt.success` in `process_delivery_receipt` before rewarding and clearing the order, e.g.:
```rust
ensure!(receipt.success, Error::<T>::MessageDeliveryFailed);
```
and decide the correct behavior for `success == false` (e.g., keep the order for retry, or route to a distinct non-reward cleanup path) rather than treating failed and successful deliveries identically.

### Proof of Concept
1. A message is enqueued via `do_process_message`, producing a `PendingOrder { nonce, fee, .. }` with `fee > 0` [4](#0-3) .
2. On Ethereum, the corresponding inbound dispatch fails, and the gateway contract emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. A relayer obtains a valid proof for this log and calls `submit_delivery_receipt`. `T::Verifier::verify` succeeds (the log/proof are legitimate), and `DeliveryReceipt::try_from` decodes `success: false` without error.
4. `process_delivery_receipt` checks `receipt.gateway` matches and `order.fee > 0`, then unconditionally calls `T::RewardPayment::register_reward(&reward_account, ..., order.fee)` and removes the order — the relayer is fully rewarded for a delivery that Ethereum itself reported as failed.

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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L300-317)
```rust
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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L445-481)
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
	}
```
