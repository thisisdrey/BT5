The code confirms the claim exactly as described. `process_delivery_receipt` at lines 446-480 checks only `T::GatewayAddress::get() == receipt.gateway` and looks up `PendingOrders::get(nonce)`, but never inspects `receipt.success` before unconditionally calling `T::RewardPayment::register_reward` and removing the `PendingOrders` entry via `<PendingOrders<T>>::remove(nonce)`.The confirmation search shows zero references to `success` anywhere in the outbound-queue-v2 pallet, proving the field is decoded in the primitives crate but never consulted in the pallet logic. The claim is fully validated against the actual code.

Audit Report

## Title
Relayer reward paid on `submit_delivery_receipt` regardless of `DeliveryReceipt.success` value - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

## Summary
`Pallet::process_delivery_receipt` in the Snowbridge outbound-queue-v2 pallet pays the relayer reward and removes the `PendingOrders` entry for a nonce without ever checking the `success: bool` field of the decoded `DeliveryReceipt`. As a result, a delivery receipt reporting that the Ethereum-side message execution failed is settled identically to one reporting success, causing unconditional reward payout and permanent, irrecoverable closure of the pending order.

## Finding Description
The extrinsic `submit_delivery_receipt` verifies the Ethereum log/proof via `T::Verifier::verify`, decodes it into a `DeliveryReceipt` (which explicitly carries `success: bool`, sourced from the on-chain `InboundMessageDispatched(uint64 indexed nonce, bytes32 topic, bool success, bytes32 reward_address)` event), and forwards it to `Self::process_delivery_receipt(relayer, receipt)` [1](#0-0) .

`process_delivery_receipt` only checks that `receipt.gateway` matches `T::GatewayAddress::get()` and that a matching `PendingOrders` entry exists for `receipt.nonce`; it then unconditionally calls `T::RewardPayment::register_reward(&reward_account, T::DefaultRewardKind::get(), order.fee)` and removes the order via `<PendingOrders<T>>::remove(nonce)`, regardless of `receipt.success` [2](#0-1) . A search of the entire pallet confirms `receipt.success` is never referenced anywhere in `outbound-queue-v2`, meaning the field decoded in `DeliveryReceipt::try_from` is discarded before it reaches settlement logic. This breaks the invariant that receipts and payout state must only advance after execution success is confirmed — settlement here advances even when the Ethereum-side event explicitly reports `success = false`.

## Impact Explanation
This allows a relayer reward (`order.fee`) to be paid out and the corresponding `PendingOrders[nonce]` entry to be permanently removed even when the underlying Ethereum message execution failed. There is no retry or second-chance path visible once the order is removed, so this is a duplicate/incorrect settlement outcome and an unbacked payout of protocol reward funds for work that was not actually completed, matching the "duplicate settlement or payout" and reward-payout-without-atomic-success impact classes.

## Likelihood Explanation
The path is reachable by any unprivileged relayer via the public, signed extrinsic `submit_delivery_receipt` — `ensure_signed(origin)` is the only origin check, and no privileged role or malicious-validator assumption is required [3](#0-2) . Any legitimate relayer whose relayed message execution reverts on the Ethereum Gateway (gas exhaustion, downstream call failure, etc.) can submit the genuine `success=false` receipt and still collect the fee, since `process_delivery_receipt` provides no gate on `receipt.success`.

## Recommendation
Branch on `receipt.success` in `process_delivery_receipt` before paying the reward and/or removing the `PendingOrders` entry: on failure, withhold or reduce the reward, keep the order open for a future correct receipt, or emit a distinct failure event (e.g., `MessageDeliveryFailed`) instead of unconditionally emitting `Event::MessageDelivered` and settling the reward.

## Proof of Concept
1. A message is enqueued via `do_process_message`, creating a `PendingOrder { nonce, fee, block_number }` entry [4](#0-3) .
2. Execution of the message on the Ethereum Gateway reverts/fails; the Gateway still emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. A relayer captures this genuine log/proof and calls `submit_delivery_receipt(origin, event)`; `T::Verifier::verify` succeeds and `DeliveryReceipt::try_from` decodes `success=false` successfully.
4. `process_delivery_receipt` checks only `gateway` and `PendingOrders::get(nonce)` — both pass — then unconditionally pays `order.fee` to `reward_account` via `T::RewardPayment::register_reward` and removes the pending order, exactly as it would for a successful delivery [5](#0-4) .
5. A unit test can assert: construct a `DeliveryReceipt` with `success: false`, call `process_delivery_receipt`, and observe that `T::RewardPayment::register_reward` is still invoked and `PendingOrders::get(nonce)` returns `None` afterward — demonstrating the missing `success` check.

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
