Audit Report

## Title
Relayer reward and order settlement ignore the on-chain `success` flag in `DeliveryReceipt` - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

## Summary
`DeliveryReceipt::try_from` decodes the `success: bool` field from the Ethereum `InboundMessageDispatched` event log, but `process_delivery_receipt` never reads or checks it before paying the relayer reward and removing the `PendingOrder`. This allows a relayer to submit a proof of a failed (reverted) message dispatch and still be paid as if delivery succeeded, with the order permanently cleared and no retry path.

## Finding Description
`submit_delivery_receipt` verifies the Merkle/execution proof that an `InboundMessageDispatched` log genuinely exists in a finalized Ethereum block via `T::Verifier::verify`, then decodes it into a `DeliveryReceipt` struct that includes `success`, and forwards it to `process_delivery_receipt` without any inspection of that field [1](#0-0) .

Inside `process_delivery_receipt`, the only validation performed is that `receipt.gateway` matches `T::GatewayAddress`; the code then unconditionally looks up the `PendingOrder` by `receipt.nonce`, pays `order.fee` to the reward account via `T::RewardPayment::register_reward`, and removes the order — regardless of `receipt.success` [2](#0-1) . The `success` field is decoded onto the `DeliveryReceipt` struct [3](#0-2)  but is dropped/unused after that point.

The `PendingOrder` (containing the fee owed) is created when the message is originally queued in `do_process_message` [4](#0-3) , and this is the only place the order is later resolved — there is no separate failure-handling path, no requeue, and no distinct event for failed vs. successful delivery.

## Impact Explanation
Because the reward payout and removal of `PendingOrders[nonce]` in `process_delivery_receipt` are unconditional on `receipt.success`, a real (but failed/reverted) Ethereum dispatch produces a proof that is accepted identically to a successful one. This results in unbacked/incorrect reward payout from the bridge reward funds and permanent, unrecoverable settlement of a message delivery order that never actually succeeded — matching the "theft or unbacked... payout" / "message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically" pivot. This is achievable by any signed relayer account calling the public `submit_delivery_receipt` extrinsic; no privileged, validator, or governance role is required.

## Likelihood Explanation
High. Any signed account can call `submit_delivery_receipt` with a legitimate proof for a nonce whose Ethereum-side execution reverted (`success = false`). The verifier only checks the log's authenticity/inclusion, not its semantic outcome, so `process_delivery_receipt`'s missing `success` check is directly and repeatably reachable through the standard public extrinsic path.

## Recommendation
In `process_delivery_receipt`, branch on `receipt.success`: only pay the reward and clear the order on `success == true`; on `success == false`, define an explicit failure path (e.g., reject/no-op leaving the order intact for redelivery, or an explicit requeue/refund mechanism) instead of treating it identically to a successful delivery.

## Proof of Concept
1. A message is queued via `do_process_message`, inserting `PendingOrders[nonce] = { fee, .. }` [5](#0-4) .
2. On Ethereum, the gateway dispatch for that nonce reverts, emitting `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. A relayer builds the standard event/proof (`EventProof`) for that transaction and calls `submit_delivery_receipt(origin, event)`.
4. `T::Verifier::verify` succeeds (log genuinely included); `DeliveryReceipt::try_from` decodes `success = false`.
5. `process_delivery_receipt` ignores `receipt.success`, calls `T::RewardPayment::register_reward(&reward_account, .., order.fee)`, and executes `<PendingOrders<T>>::remove(nonce)` [6](#0-5)  — identical to the successful-delivery case, confirming the reward and order-removal state transitions are not gated by delivery outcome.

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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L452-480)
```rust
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

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/delivery_receipt.rs (L44-50)
```rust
		Ok(Self {
			gateway: log.address,
			nonce: event.nonce,
			topic: H256::from_slice(event.topic.as_ref()),
			success: event.success,
			reward_address: event.reward_address.0,
		})
```
