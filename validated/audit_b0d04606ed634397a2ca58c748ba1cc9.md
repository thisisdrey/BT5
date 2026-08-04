### Title
Relayer reward paid on Snowbridge outbound delivery receipt regardless of on-chain execution outcome — `success` flag ignored - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
`Pallet::process_delivery_receipt` in the Snowbridge outbound-queue-v2 pallet registers and pays out the relayer reward for a delivered message purely based on the existence of a `PendingOrder` and the `fee` recorded in it. It never inspects the `success` field of the decoded `DeliveryReceipt`, even though that field exists specifically to indicate whether the Ethereum-side `InboundMessageDispatched` execution succeeded.

### Finding Description
The `DeliveryReceipt` struct decoded from the Ethereum `InboundMessageDispatched` event log carries a `success: bool` field: [1](#0-0) 

However, `process_delivery_receipt` never reads or branches on `receipt.success`. It only validates the gateway address, resolves the reward account, fetches the `PendingOrder` by nonce, and — as long as `order.fee > 0` — unconditionally calls `T::RewardPayment::register_reward` before removing the order and emitting `MessageDelivered`: [2](#0-1) 

This is the same class of bug as the external report's "unchecked ERC20 transfer": an operation whose outcome (`success`) is known but discarded, causing a "successful settlement" event/state transition (`MessageDelivered`, reward registration, and permanent removal of `PendingOrders`) to be emitted/committed even when the underlying delivery on the destination side failed. Once `PendingOrders::remove(nonce)` executes, the order can never be resubmitted or reconciled — the relayer is paid and the record is gone regardless of whether Ethereum execution reverted.

### Impact Explanation
This falls under "duplicate settlement or payout" / "message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically" from the Polkadot SDK pivots. A relayer reward is a real, unbacked (from the sending chain's perspective, reward-pot funded) payout; paying it out for failed deliveries drains the reward pot for work that did not actually complete, and it permanently discards the pending order so there is no way to detect or later react to the failure via this code path.

### Likelihood Explanation
The condition is trivially reachable: any relayer who submits a valid, well-formed proof for an `InboundMessageDispatched` event where `success == false` (e.g., the destination-side dispatch reverted due to an out-of-gas/execution error unrelated to the relayer's honesty) receives the same reward path as a genuinely successful delivery. No malicious peer, validator, governance action, or leaked key is required — an ordinary relayer submitting a legitimately-proven but failed-execution receipt triggers the unconditional payout.

### Recommendation
Branch on `receipt.success` before calling `register_reward`: pay/register the relayer reward (and possibly a smaller "delivery attempt" fee) only when `success == true`; on `false`, either withhold the fee reward entirely, emit a distinct `MessageDeliveryFailed`/`InboundExecutionFailed` event, and decide deterministically whether the order should be retried, refunded, or cleared without payout. At minimum, do not conflate "receipt proven" with "message executed successfully" when deciding to advance payout state.

### Proof of Concept
1. Relayer relays a message to Ethereum; on the Ethereum gateway contract, execution of the inbound command reverts, and the gateway emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
2. Relayer obtains a valid transaction-receipt/proof for this event log (it is a real, honestly emitted event — no forgery needed).
3. Relayer calls `submit_delivery_receipt` (which invokes `process_delivery_receipt`) with this receipt.
4. `process_delivery_receipt` decodes `DeliveryReceipt{ success: false, ... }`, looks up `PendingOrders[nonce]`, sees `order.fee > 0`, and calls `T::RewardPayment::register_reward(&reward_account, ..., order.fee)` — paying the relayer as if delivery succeeded.
5. `PendingOrders::remove(nonce)` is called and `Event::MessageDelivered { nonce }` is emitted, permanently closing out the order despite the failed execution on Ethereum. [3](#0-2)

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
