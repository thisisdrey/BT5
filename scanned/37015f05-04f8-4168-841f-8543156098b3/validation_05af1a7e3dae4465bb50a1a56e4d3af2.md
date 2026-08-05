## Title
Relayer reward for Ethereum→Polkadot delivery is paid without checking dispatch `success`, allowing settlement to advance on failed/irrelevant outcomes - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
The external report's core broken invariant is: *a state-advancing check is performed against the wrong/insufficiently-specific field, letting an unprivileged actor cause the guarded resource to be consumed/settled without the intended condition being satisfied* (cap checked against `total` instead of `locked`). The closest local analog is in Snowbridge's outbound-queue-v2 pallet: `process_delivery_receipt` decodes a `DeliveryReceipt` that carries a `success: bool` field describing whether the message dispatch on Ethereum actually succeeded, but this field is never consulted before the pending order is settled and the relayer reward is paid out.

### Finding Description
`DeliveryReceipt` is decoded from the `InboundMessageDispatched` event log and explicitly carries `success`: [1](#0-0) 

`process_delivery_receipt` verifies only the gateway address and looks up the `PendingOrder` by `nonce`. It never reads `receipt.success` before paying the reward and removing the order: [2](#0-1) 

The module doc explicitly states the intended flow requires verifying "the message with proof ... same as the inbound queue verification flow" and then paying the reward "attached in the order" — it does not describe conditioning payment on dispatch success, yet the presence of a dedicated `success` field in the wire format strongly signals that downstream consumers are expected to distinguish successful vs failed dispatch outcomes: [3](#0-2) 

Because `success` is decoded but discarded, the settlement state machine ("message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically") advances (order removed, `MessageDelivered` emitted, reward registered) purely on the basis of a valid Merkle/receipt proof for *any* `InboundMessageDispatched` log matching the nonce — regardless of whether the encoded outcome was a revert. The existing guards (`GatewayAddress` check, nonce→`PendingOrders` lookup, verifier proof check in `submit_delivery_receipt`) only bind chain/gateway/nonce identity; none of them bind the settlement action to the dispatch outcome carried in the payload itself.

### Impact Explanation
If downstream logic (fee refunds, XCM callbacks, or off-chain accounting) or future extensions rely on `DeliveryReceipt.success` to distinguish a completed transfer from a reverted one, the current pallet already forecloses that distinction at the point of settlement: the reward is unconditionally registered and the order is unconditionally removed the moment any receipt with a matching nonce and gateway is proven, whether or not the dispatch reverted on Ethereum. This breaks the "duplicate settlement / payout state must only advance after execution succeeds" invariant for bridge reward accounting, and removes any per-outcome signal a relayer or protocol could use to distinguish paid-for-success vs paid-for-failure delivery. At minimum, this is a real-vs-documented behavior mismatch in bridge settlement logic on BridgeHub; at worst, it silently pays relayers for delivering messages that failed on the Ethereum side, since nothing in `process_delivery_receipt` treats `success == false` differently from `success == true`.

### Likelihood Explanation
Any relayer (an unprivileged, permissionless signed origin — `submit_delivery_receipt` only requires `ensure_signed`) can trigger this path for every real dispatch that occurs on Ethereum; no malicious peer/validator/governance action is required. The condition is deterministic and always reachable whenever a message dispatch on Ethereum reverts but is still logged with `success: false` in `InboundMessageDispatched` — the relayer still gets the fee and the order is cleared exactly as in the success case.

### Recommendation
In `process_delivery_receipt` (`bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`), branch on `receipt.success` before paying/registering the reward and removing the `PendingOrder`. Either (a) reduce or withhold `order.fee` when `success == false`, or (b) explicitly document and test that reward payment is intentionally decoupled from dispatch outcome, and expose `success` in the `MessageDelivered` event so downstream consumers (fee refund logic, XCM error handling) can act on it rather than having the information dropped silently at the settlement boundary.

### Proof of Concept
1. Relayer submits an outbound message via XCM; `process_message` assigns `nonce = N` and inserts `PendingOrder { nonce: N, fee: F, .. }`.
2. Message is delivered to the Ethereum Gateway, but the encoded command execution reverts on-chain; the Gateway still emits `InboundMessageDispatched(nonce=N, topic, success=false, reward_address)`.
3. Relayer obtains a valid transaction-receipt Merkle proof for this log (this is legitimate, not forged) and calls `submit_delivery_receipt` with `EventProof { event_log, proof }`.
4. `T::Verifier::verify` succeeds (proof is genuine), `DeliveryReceipt::try_from` decodes `success = false` into the `receipt` struct.
5. `process_delivery_receipt` checks only `receipt.gateway` and `PendingOrders::get(N)`; it pays `order.fee` to the reward account and removes the order — identical to the success path — because `receipt.success` is never inspected.

I could not find any other call site in the indexed portion of the repo that consumes `receipt.success` after this point, which supports the conclusion that the field is currently write-only from the on-chain settlement perspective. If a fuller index shows additional consumers of this receipt struct that I could not surface, that would need to be verified in a live Devin session with full file access.

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
