### Title
`success` field of Snowbridge V2 delivery receipt is decoded but never checked before paying relayer reward - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
`DeliveryReceipt::try_from` decodes the `InboundMessageDispatched(nonce, topic, success, reward_address)` Ethereum event log into a `DeliveryReceipt` struct that carries a `success: bool` field indicating whether the dispatched command actually executed successfully on Ethereum. [1](#0-0)  `Pallet::process_delivery_receipt`, which is reachable from the public, unprivileged extrinsic `submit_delivery_receipt`, never reads or checks `receipt.success` anywhere in its logic before paying out the relayer's reward. [2](#0-1) [3](#0-2)  This is a direct structural analog to the seed report: a boolean success flag returned/emitted by an external call chain is decoded but silently discarded instead of being used to gate the follow-on action (there: staking; here: reward payout).

### Finding Description
`submit_delivery_receipt` is a signed, permissionless extrinsic — any account can call it. [4](#0-3)  After the Ethereum event-log proof is verified by `T::Verifier::verify` (which only proves that the log was included and well-formed, not that the underlying dispatched command succeeded), the log is decoded into a `DeliveryReceipt`, capturing `success` as decoded straight from the emitted `InboundMessageDispatched` event. [5](#0-4) 

`process_delivery_receipt` then:
1. Checks the log's `gateway` address matches the configured `GatewayAddress`.
2. Resolves the `reward_account` (attacker-controlled — defaults to caller unless `reward_address` is set inside the same Ethereum event payload the relayer/attacker controls the trigger for).
3. Looks up the `PendingOrder` by nonce.
4. Pays the `order.fee` via `T::RewardPayment::register_reward` if `fee > 0`.
5. Removes the pending order and emits `MessageDelivered`.

At no point is `receipt.success` inspected. [6](#0-5)  Whether the dispatched command reverted or executed successfully on Ethereum, the reward is paid identically as long as the event log is valid and the nonce is still pending. A `grep` across the whole `outbound-queue-v2` pallet confirms `success` is never referenced anywhere except inside the primitive decode struct itself — it is a genuinely dead/ignored field in the payout path.

This mirrors the seed bug exactly: an external call/message legitimately returns a boolean success indicator, but the caller proceeds with state-changing effects (staking there, reward settlement here) without gating on that boolean, defeating the very purpose of the flag and allowing the "failure" path to be treated identically to "success."

### Impact Explanation
Because reward settlement is decoupled from actual successful message dispatch on Ethereum, a relayer can obtain full relay rewards for messages whose commands revert or fail on the Ethereum side, as long as they can produce a valid inclusion proof of the `InboundMessageDispatched` event (with `success = false`). This breaks the "message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically" invariant for the bridge — the fee/reward accounting here treats "message emitted an event" as equivalent to "message successfully processed," which can drain the reward pool for a non-productive/failed workload (public underpriced work degrading correct bridge accounting) and creates an incentive to intentionally submit messages that revert on Ethereum cheaply while still collecting BridgeHub-side rewards.

### Likelihood Explanation
High: `submit_delivery_receipt` requires only `ensure_signed` (any account) and a normal proof of a real Ethereum event log — no governance, no malicious validator/collator/relayer trust assumption is needed beyond the relayer being the ordinary, expected caller of this extrinsic. [4](#0-3)  Triggering a command that reverts on the Ethereum Gateway (e.g., due to insufficient gas budget, a malformed but proof-passing payload, or a legitimately-failing downstream call) is plausible in normal operation, and the relayer submitting the (valid) proof of that failed dispatch still collects the reward under current code, since `success` is dropped on the floor.

### Recommendation
Check `receipt.success` in `process_delivery_receipt` before crediting `order.fee`, e.g.:
```rust
ensure!(receipt.success, Error::<T>::DeliveryFailed);
if order.fee > 0 {
    T::RewardPayment::register_reward(&reward_account, T::DefaultRewardKind::get(), order.fee);
}
```
or, if the intent is that relayers should always be paid for delivering the proof (only the downstream command failed, not the relaying), explicitly document and test that decision, and consider a reduced/no reward path keyed off `receipt.success` so that mere event-log delivery cannot be conflated with successful message execution for accounting/incentive purposes.

### Proof of Concept
1. A cross-chain command dispatched via the outbound queue V2 pipeline reaches Ethereum and its Gateway contract execution reverts (e.g., insufficient gas allotted, or the destination contract call fails) — the Ethereum Gateway still emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
2. Any signed account observes this event, obtains a normal transaction-receipt/event-log proof for it, and calls `submit_delivery_receipt(origin, event)` on BridgeHub. [4](#0-3) 
3. `T::Verifier::verify` succeeds (the log is genuinely included), `DeliveryReceipt::try_from` decodes `success = false` correctly, but `process_delivery_receipt` proceeds to pay the full `order.fee` to `reward_account` regardless, exactly as it would for `success = true`. [6](#0-5) 
4. `PendingOrders` is cleared and `MessageDelivered` is emitted, indistinguishable from a genuinely successful delivery — the reward has been paid for a failed cross-chain execution.

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
