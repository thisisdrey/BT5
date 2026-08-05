This confirms the claim exactly. The code at `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs` L445-480 in `process_delivery_receipt` never reads `receipt.success` — it only checks `T::GatewayAddress::get() == receipt.gateway` and `order.fee > 0` before unconditionally calling `T::RewardPayment::register_reward` and removing the `PendingOrder`. The `DeliveryReceipt` struct decoded from the Ethereum `InboundMessageDispatched` event does carry a `success: bool` field <cite repo="Lauraivanka/polkadot-sdk--018" path="bridges/snowbridge/primitives/outbound-queue/src/v2/delivery_receipt.rs" start="10-27" end="10-27" /> that is fully populated during decode [1](#0-0)  but is never referenced anywhere in `process_delivery_receipt`'s logic [2](#0-1) .

Audit Report

## Title
Relayer reward paid unconditionally regardless of on-chain delivery `success` flag - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

## Summary
`DeliveryReceipt` decoded from the Ethereum Gateway's `InboundMessageDispatched` event carries a `success: bool` field indicating whether the relayed message actually executed successfully on Ethereum, but `Pallet::process_delivery_receipt` never inspects this field before registering the relayer reward and permanently removing the `PendingOrder`. This means a relayer who submits a valid receipt proof for a message that failed/reverted on Ethereum still receives the full fee, and the order is discarded with no possibility of retry or reconciliation.

## Finding Description
The module doc explicitly states the reward should be paid "when the message has been verified and executed" (`bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs` L36-41), implying payout should be conditioned on successful execution. However `process_delivery_receipt` only validates the gateway address (`ensure!(T::GatewayAddress::get() == receipt.gateway, ...)`), resolves the reward account, looks up the `PendingOrders` entry by `nonce`, and pays the reward purely based on `order.fee > 0`, then unconditionally removes the order and emits `MessageDelivered`:

```rust
let order = <PendingOrders<T>>::get(nonce).ok_or(Error::<T>::InvalidPendingNonce)?;
if order.fee > 0 {
    T::RewardPayment::register_reward(&reward_account, T::DefaultRewardKind::get(), order.fee);
}
<PendingOrders<T>>::remove(nonce);
Self::deposit_event(Event::MessageDelivered { nonce });
```

`receipt.success` is decoded from the on-chain Ethereum log via `DeliveryReceipt::try_from` in `bridges/snowbridge/primitives/outbound-queue/src/v2/delivery_receipt.rs` L44-50, but the field is dead data as far as `process_delivery_receipt` is concerned — no existing guard checks it. The only checks performed are gateway-address equality and the presence of a `PendingOrders` entry, neither of which relate to whether the dispatched message actually executed successfully.

## Impact Explanation
Any relayer who submits a delivery receipt corresponding to a failed Ethereum execution (`success = false`) still collects the full fee from the reward pool, and the associated `PendingOrder` is irrevocably removed with no route for retry or reconciliation. This is an unbacked payout — value leaves the bridge relayer reward pot without the corresponding successful delivery it is meant to compensate for — matching the "duplicate settlement or payout" / unbacked-payout impact class, since the queue/payout state advances (order removed, reward registered) without the required successful execution.

## Likelihood Explanation
`submit_delivery_receipt` is a public, unprivileged extrinsic callable by any signed account with a valid Ethereum proof (`bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs` L298-317). The only condition needed to trigger the bug is that the relayed message's execution on the Ethereum Gateway contract fails/reverts while the Gateway still emits `InboundMessageDispatched` with `success = false` — a normal, expected occurrence for any bridge (gas misestimation, downstream command revert, etc.), not requiring any malicious or privileged actor.

## Recommendation
Check `receipt.success` in `process_delivery_receipt` before registering the reward. On `success == false`, withhold or reduce the reward and either retain the `PendingOrder` in a separate "failed" state for retry/reconciliation rather than removing it identically to the successful path.

## Proof of Concept
1. `do_process_message` enqueues a message, creating `PendingOrder { nonce, fee, .. }` with `fee > 0` (`lib.rs` L426-436).
2. The message is delivered to the Ethereum Gateway, but execution reverts/fails; the Gateway still emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. A relayer submits `submit_delivery_receipt` with the resulting proof; `Verifier::verify` succeeds since the log genuinely exists, and `DeliveryReceipt::try_from` decodes `success = false`.
4. `process_delivery_receipt` ignores `success`, pays `order.fee` via `T::RewardPayment::register_reward`, removes the `PendingOrder`, and emits `MessageDelivered { nonce }` — identical to the successful-delivery path exercised in the integration test `snowbridge_v2_outbound.rs` L103-122, which only differs by setting `success: true`.

### Citations

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
