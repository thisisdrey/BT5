### Title
Relayer Reward Paid and Pending Order Cleared Regardless of Ethereum Execution Outcome - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
The external report's core broken invariant is: a payout/administrative parameter is fixed and never validated against the actual outcome of the operation it is supposed to be conditioned on (fee is hardcoded to 0, with no path to correct it once real conditions change). The local analog in `pallet_outbound_queue_v2` is structurally similar but has a direct fund-safety consequence: the pallet decodes a `success` flag from the Ethereum delivery-receipt log but never uses it to gate reward payment or order settlement, so the "settlement must only advance after... execution succeeds" invariant required by the Snowbridge pivots is violated.

### Finding Description
`DeliveryReceipt` (decoded from the Ethereum `InboundMessageDispatched` event) carries a `success: bool` field indicating whether the inbound message actually executed successfully on Ethereum: [1](#0-0) 

`Pallet::process_delivery_receipt`, which is invoked from the `submit_delivery_receipt` extrinsic pipeline described in the module docs, only verifies the gateway address and the existence of a `PendingOrder` for the nonce. It never inspects `receipt.success` before paying the reward and clearing the order: [2](#0-1) 

The module-level documentation itself confirms the intended flow is "verify proof → fetch pending order → pay reward → remove order," with no mention of checking dispatch success: [3](#0-2) 

This breaks the required invariant from the pivot guidance that "message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically." Here, settlement (reward payment) and state advancement (`PendingOrders::remove`) both proceed even when `success == false`.

### Impact Explanation
Any relayer who submits a valid, correctly-proven `InboundMessageDispatched` event — including one where the on-chain command execution reverted on Ethereum (`success = false`) — still collects the full `order.fee` reward via `T::RewardPayment::register_reward`, and the `PendingOrders` entry is removed. This is a genuine over-payment / incorrect-settlement bug: work that did not successfully complete is paid as if it had. It also means there is no way to retry or re-settle a failed order, since the pending order is deleted regardless of outcome, leading to a permanent inconsistency between the "delivered and executed" state Polkadot believes exists and the actual Ethereum state (Ethereum-side effects that failed are silently accepted as settled on the Polkadot side). This is a direct instance of "public underpriced work" / duplicate-or-incorrect settlement affecting bridge reward accounting, in scope per the pivot on message queues and payout state.

### Likelihood Explanation
No malicious relayer, validator, or governance action is required — this triggers under normal, permissionless relaying whenever a legitimately-relayed inbound command fails execution on Ethereum for any reason (insufficient destination gas, an XCM/command that reverts, a decode error on the Ethereum side, etc.). The relayer submitting the receipt is not required to have caused the failure; they only need to submit the (real, provable) receipt for a message whose execution failed. Given that Ethereum execution outcomes for arbitrary commands cannot be perfectly guaranteed in advance, this is a realistically reachable path, not a contrived edge case.

### Recommendation
Check `receipt.success` in `process_delivery_receipt` before paying out the reward. On failure, either withhold/reduce the reward, keep (or requeue) the pending order for retry/dispute handling, or route the fee to a distinct "failed delivery" accounting path instead of unconditionally rewarding the relayer and deleting the order.

### Proof of Concept
1. A message is enqueued and processed by the outbound queue, creating `PendingOrders[nonce]` with `fee = F` as described in the doc comment at `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs:21-26`.
2. A relayer relays the message to Ethereum; execution of the command reverts/fails on the Ethereum Gateway contract, which nonetheless emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)` (per the Solidity event signature at `bridges/snowbridge/primitives/outbound-queue/src/v2/delivery_receipt.rs:11`).
3. The relayer obtains a valid receipt/header proof for this event and calls `submit_delivery_receipt`, which decodes it into a `DeliveryReceipt{ success: false, .. }` and calls `process_delivery_receipt`.
4. In `process_delivery_receipt` (`bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs:446-480`), the code checks only `receipt.gateway` and `PendingOrders::get(nonce)`; `receipt.success` is decoded upstream but not read here. `order.fee > 0` triggers `T::RewardPayment::register_reward(&reward_account, ..., order.fee)`, and `PendingOrders::remove(nonce)` executes unconditionally.
5. Result: the relayer is rewarded `F` and the order is permanently cleared, even though the corresponding Ethereum-side command execution failed — settlement advanced without confirmed successful execution.

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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L36-41)
```rust
//! 10. When the message has been verified and executed, the relayer will call the extrinsic
//!     `submit_delivery_receipt` to:
//! 	a. Verify the message with proof for a transaction receipt containing the event log,
//! 	   same as the inbound queue verification flow
//! 	b. Fetch the pending order by nonce of the message, pay reward with fee attached in the order
//!    	c. Remove the order from `PendingOrders` map storage by nonce
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
