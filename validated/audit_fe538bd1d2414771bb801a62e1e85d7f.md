## Finding: Delivery Receipt `success` Flag Ignored When Settling Snowbridge Outbound Orders

The external report's core defect is a strict, disconnected verification check (`oracleTolerableLimit == 0` between independently-priced values) that lets a legitimate close-out settle *incorrectly* — the settlement path advances without properly reflecting whether the underlying operation actually succeeded, risking stuck/mismatched state. The closest verifiable local analog is in Snowbridge's outbound queue v2 delivery-receipt settlement: the pallet decodes a `success` flag straight from the Ethereum `InboundMessageDispatched` event but never checks it before finalizing payout and removing the pending order.

### Title
Outbound Queue V2 settles and rewards delivery regardless of on-chain execution `success` flag - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
`Pallet::process_delivery_receipt` decodes a `DeliveryReceipt` (which includes a `success: bool` field taken directly from the Ethereum `InboundMessageDispatched` event) but never inspects that field. It unconditionally pays the relayer's fee reward and removes the `PendingOrder`, emitting `MessageDelivered`, whether or not the corresponding commands actually executed successfully on Ethereum.

### Finding Description
The Ethereum Gateway contract emits `InboundMessageDispatched(nonce, topic, success, reward_address)` specifically to let the relay/settlement logic distinguish between successful and failed command execution. This is decoded into `DeliveryReceipt.success` on the Substrate side [1](#0-0) .

However, `process_delivery_receipt` never reads `receipt.success`. It only checks the gateway address, looks up the `PendingOrder` by nonce, pays the fee, and removes the order — treating any valid receipt as final settlement regardless of the execution outcome: [2](#0-1) 

The pallet doc explicitly states the receipt flow is meant to "verify the message... fetch the pending order by nonce... pay reward with fee attached in the order... remove the order from `PendingOrders`" — with no mention of distinguishing success/failure, confirming the check was dropped rather than intentionally omitted [3](#0-2) . Existing guards (`GatewayAddress` equality check, `PendingOrders` existence check) only validate provenance and nonce uniqueness — neither stops a receipt reporting `success == false` from settling the order exactly like a successful one.

### Impact Explanation
Once `submit_delivery_receipt` is accepted for a given nonce, `PendingOrders::<T>::remove(nonce)` is permanent and `MessageDelivered` is emitted unconditionally [4](#0-3) . Any downstream consumer of `MessageDelivered` (or of the absence of a pending order) will treat the outbound message as fully and successfully executed on Ethereum. If the underlying commands (e.g., asset unlock/mint on Ethereum) actually reverted, there is no retry or reconciliation path — the message is irreversibly marked "delivered" even though the intended cross-chain effect never happened. This falls under "message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically" and can produce a permanent bridge-state/fund-accounting mismatch (assets considered settled on the Substrate side with no corresponding effect on Ethereum, and no automatic remediation).

### Likelihood Explanation
This does not require a malicious relayer, validator, or governance action — it is triggered by ordinary command execution failures on the Ethereum side (e.g., insufficient gas estimation from `GasMeter::maximum_dispatch_gas_used_at_most`, a reverting downstream contract call, or any legitimate `success=false` outcome), combined with a normal, honest relayer submitting the resulting receipt exactly as instructed by the documented flow. No test in `outbound-queue-v2/src/test.rs` exercises the `success == false` case, and all constructed test receipts hardcode `success: true`, consistent with this path being unchecked and unexercised.

### Recommendation
In `process_delivery_receipt`, branch on `receipt.success`: only pay reward and finalize/remove the `PendingOrder` on `true`; on `false`, emit a distinct failure event (e.g., `MessageDeliveryFailed`) and either retain the order for reconciliation/refund handling or route to an explicit failure-settlement path so downstream state cannot assume success.

### Proof of Concept
1. An outbound message is queued via `do_process_message`, creating a `PendingOrder { nonce, fee, block_number }`.
2. On Ethereum, the Gateway attempts the commands but they revert (e.g., insufficient forwarded gas); the Gateway still emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. A relayer generates a valid proof for this event and calls `submit_delivery_receipt`.
4. `T::Verifier::verify` succeeds (the proof is valid — it just proves a *failed* execution), `DeliveryReceipt::try_from` decodes `success: false`.
5. `process_delivery_receipt` ignores `success`, pays `order.fee` to the reward account, removes `PendingOrders[nonce]`, and emits `MessageDelivered { nonce }` — identical to the successful case, even though the intended Ethereum-side effect never occurred.

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
