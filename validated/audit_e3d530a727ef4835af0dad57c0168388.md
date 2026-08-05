Confirmed: `process_delivery_receipt` at [1](#0-0)  validates only `receipt.gateway` and the existence of a `PendingOrders` entry, then unconditionally calls `T::RewardPayment::register_reward` and removes the order — `receipt.success` is never read. The `DeliveryReceipt` struct genuinely carries a `success: bool` decoded straight from the on-chain `InboundMessageDispatched` event, as confirmed by the primitives file. This matches the claim exactly as submitted, with no scope-widening needed.

Audit Report

## Title
Unchecked `DeliveryReceipt.success` flag allows relayer reward payout for failed message execution - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

## Summary
`process_delivery_receipt` decodes a `DeliveryReceipt` (via `DeliveryReceipt::try_from`) that includes a `success: bool` field reporting whether the corresponding message actually executed successfully on Ethereum. The pallet verifies the cryptographic proof of the event log, but never inspects `receipt.success` before paying the pending relayer reward, so proofs of *failed* executions are treated identically to proofs of successful ones for reward purposes.

## Finding Description
The `submit_delivery_receipt` extrinsic verifies event-log authenticity via `T::Verifier::verify`, decodes the log into a `DeliveryReceipt`, and calls `process_delivery_receipt`. That function checks only `receipt.gateway` and looks up `PendingOrders` by `receipt.nonce`; it pays `order.fee` to the reward account and removes the order unconditionally whenever those two checks pass — `receipt.success` is decoded but discarded, as confirmed in the current code at [2](#0-1) . This mirrors the classic pattern where a boolean success return is produced by an operation but the caller never checks it before proceeding with dependent state changes (crediting a payout).

## Impact Explanation
Because the Ethereum-side execution status is never validated, a relayer can submit a proof for a genuine `InboundMessageDispatched(nonce, topic, success=false, reward_address)` event — i.e., a message whose execution on Ethereum actually failed/reverted — and still collect the full relayer fee from `PendingOrders`, and the order is removed as if delivery succeeded. This is an unbacked/duplicate-condition reward payout: fee funds are settled to a relayer for work that did not achieve its intended outcome, and legitimate future re-delivery or reconciliation of the failed message loses its associated fee since the order is deleted. This falls under "bridge rewards ... must conserve value and settle exactly once to the rightful beneficiary and amount" / "duplicate settlement or payout."

## Likelihood Explanation
No privileged role is required — any unpermissioned account can call `submit_delivery_receipt` via `ensure_signed(origin)?` (confirmed at [3](#0-2) ) with a genuinely valid proof for a `success=false` event that naturally occurs whenever the destination-side call reverts (e.g., gas griefing, execution errors) on the Ethereum gateway. No malicious relayer/validator/prover collusion is needed — the attacker merely submits the truthful proof of a failed delivery to claim the reward anyway.

## Recommendation
In `process_delivery_receipt`, explicitly branch on `receipt.success`: only pay `order.fee` and treat delivery as settled when `success == true`; for `success == false`, either withhold the reward, requeue/allow re-relay, or otherwise avoid deleting the `PendingOrders` entry as if delivery had succeeded.

## Proof of Concept
1. A message is queued via `do_process_message`, creating a `PendingOrder { nonce, fee, .. }` with `fee > 0`, as seen at [4](#0-3) .
2. On Ethereum, the corresponding call to the Gateway executes but reverts/fails, so the emitted `InboundMessageDispatched` event has `success = false`.
3. Any signed relayer submits `submit_delivery_receipt` with a valid inclusion proof of that event.
4. `T::Verifier::verify` succeeds (proof is genuine), `DeliveryReceipt::try_from` decodes `success = false` into the struct, but `process_delivery_receipt` never inspects it — the reward is paid via `T::RewardPayment::register_reward` and the order removed regardless of the `success` value at [5](#0-4) .

### Citations

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
