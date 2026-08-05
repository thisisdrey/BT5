This confirms the claim precisely. The grep search shows zero occurrences of `success` anywhere in the `outbound-queue-v2` pallet, confirming `receipt.success` is decoded into the `DeliveryReceipt` struct but never referenced by `process_delivery_receipt` or any other code in the pallet.

Audit Report

## Title
Relayer reward paid regardless of `DeliveryReceipt.success`, allowing payout for failed message execution - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

## Summary
`process_delivery_receipt` decodes a `DeliveryReceipt` whose `success: bool` field records whether the corresponding message actually executed successfully on the Ethereum gateway, but the function never inspects this field before paying the relayer reward and removing the `PendingOrder`. A grep of the entire `outbound-queue-v2` pallet confirms zero references to `success`, proving the field is completely unused in the settlement logic.

## Finding Description
`DeliveryReceipt` is constructed from the `InboundMessageDispatched(nonce, topic, success, reward_address)` Ethereum event log, preserving the `success` flag verbatim: <cite repo="Kohvert/polkadot-sdk--013" path="bridges/snowbridge/primitives/outbound-queue/src/v2/delivery_receipt.rs" start="10-27" end="44-51" />.

`process_delivery_receipt` validates only `receipt.gateway` against `T::GatewayAddress` and resolves `order` via `<PendingOrders<T>>::get(nonce)`; it then unconditionally pays `order.fee` via `T::RewardPayment::register_reward` whenever `order.fee > 0`, and removes the `PendingOrder`, without ever branching on `receipt.success`: [1](#0-0) . The pallet's own doc comment describes the intended flow as "Fetch the pending order by nonce of the message, pay reward with fee attached in the order" without any success-conditional language, matching the omission: <cite repo="Kohvert/polkadot-sdk--013" path="bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs" start="36-41" end="36-41" />.

The `PendingOrder` is originally inserted when the message is queued for delivery, associating `nonce`, `fee`, and `block_number`, with the comment explicitly noting "only after that [delivery proof] the order can be resolved and the fee will be rewarded to the relayer" — again with no mention of gating on the actual dispatch outcome: [2](#0-1) .

## Impact Explanation
This violates the settlement invariant that payout state must only advance after execution succeeds. Any legitimately relayed proof for a real Ethereum-side event where `success == false` (e.g., destination gas exhaustion or an application-level revert unrelated to relayer behavior) still results in `T::RewardPayment::register_reward` paying `order.fee` to `reward_account`, and the `PendingOrders[nonce]` entry is permanently removed, foreclosing any future correct resolution of that nonce. This is a public underpriced-work / incorrect-payout bug that pays for work whose actual on-chain outcome does not match the fee's intended compensation, and it corrupts the `PendingOrders` map by irrecoverably deleting an order that should have been retried, refunded, or handled distinctly.

## Likelihood Explanation
Message dispatch failures on Ethereum (insufficient gas, reverted calls) are a normal, expected occurrence, not an attacker precondition. Any relayer submitting a valid Merkle/beacon proof for a genuine `InboundMessageDispatched` event with `success = false` via the permissionless `submit_delivery_receipt` extrinsic triggers the incorrect payout — no privileged access, malicious behavior, or special conditions are required beyond a normal failed execution occurring on Ethereum.

## Recommendation
In `process_delivery_receipt`, branch on `receipt.success` before calling `T::RewardPayment::register_reward`, only paying when `success == true`. Define explicit handling for `success == false` (e.g., a distinct event, no reward, and an explicit decision on whether/how the `PendingOrder` should be retried or cleaned up) rather than treating both outcomes identically.

## Proof of Concept
1. A message with `nonce = N`, `fee = F > 0` is queued, inserting `PendingOrders[N]` via the code at lines 426-438.
2. On Ethereum, the Gateway dispatch reverts, emitting `InboundMessageDispatched(N, topic, success=false, reward_address)`.
3. A relayer submits `submit_delivery_receipt` with a valid proof for this real log; verification succeeds since only `gateway` and `nonce` are checked.
4. `process_delivery_receipt` executes the code at lines 466-475 unconditionally, paying `F` to `reward_account` and removing `PendingOrders[N]`, despite `receipt.success == false`.

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
