## Title
`process_delivery_receipt` pays relayer reward without checking `DeliveryReceipt.success` - ([File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs])

### Summary
This is a structural analog of the reported bug: a boolean flag that is supposed to gate whether funds should move (`isRelockAction` in the MultiFeeDistribution report) exists in the code but is never consulted at the decision point, so the payout path executes unconditionally regardless of the flag's true meaning. In `snowbridge-pallet-outbound-queue-v2`, the `DeliveryReceipt` decoded from the Ethereum `InboundMessageDispatched` event carries a `success: bool` field explicitly meant to indicate whether the message dispatch on Ethereum succeeded or failed, but `Pallet::process_delivery_receipt` never reads or checks it before rewarding the relayer.

### Finding Description
The Solidity event emitted by the Gateway is declared as: [1](#0-0) 

and decoded into the `DeliveryReceipt` struct, preserving the `success` field: [2](#0-1) 

The struct field is populated in `TryFrom<&Log>`: [3](#0-2) 

However, `process_delivery_receipt` in the pallet only validates the gateway address and the existence of a `PendingOrder` for the nonce, then unconditionally registers the reward if `order.fee > 0`: [4](#0-3) 

There is no `ensure!(receipt.success, ...)` or any conditional branch on `receipt.success` anywhere in this function. The value is fully decoded and available on `receipt`, but it is discarded. This mirrors the reported bug's root cause exactly: a boolean status flag that is intended to control whether a fund-moving branch executes is not correctly wired into the "if" logic that gates the payout — in the external report the comparison was inverted (`== false` instead of `== true`); here the equivalent guard is simply absent, which is the degenerate case of the same broken-invariant class (flag exists, decision path ignores it).

The doc comment for the pallet even describes the intended flow as "pay reward with fee attached in the order" upon receipt of a delivery proof, without qualifying it on dispatch success — confirming the check was dropped rather than intentionally omitted: [5](#0-4) 

All emulated integration tests found also always construct `DeliveryReceipt { success: true, .. }` and never exercise the `success: false` path, which corroborates the absence of a check: [6](#0-5) 

### Impact Explanation
Any relayer can submit a valid Ethereum transaction receipt/proof for a message whose on-chain (Ethereum-side) execution reverted or otherwise failed (`success = false`) — the Beefy/light-client proof only attests that the event log was emitted and included in a valid block, not that the dispatched command succeeded. Because `process_delivery_receipt` ignores `receipt.success`, the relayer reward (`order.fee`) is paid out via `T::RewardPayment::register_reward` even for failed deliveries, and the `PendingOrder` is removed regardless. This is an "unbacked" payout: fee funds are settled to a relayer despite the underlying bridge action not completing successfully, i.e., a payout that does not correspond to the value/service actually delivered — directly matching the Impact Gate's "theft or unbacked mint or unlock" / "duplicate settlement or payout" criteria for Snowbridge BridgeHub code, without needing any malicious peer, relayer collusion with governance, or leaked keys — an ordinary permissionless caller (a normal relayer) submitting a real but failed-dispatch proof is sufficient.

### Likelihood Explanation
The `submit_delivery_receipt` extrinsic is a public, permissionless, unprivileged entrypoint reachable by any relayer once a message is queued and the Ethereum event is included in a finalized/verified block: an unprivileged actor only needs a real (not forged) `InboundMessageDispatched` event with `success = false`, which naturally occurs whenever the dispatched command on Ethereum reverts (e.g., insufficient gas allocation vs actual execution cost, a reentrant failure, or any Solidity-side revert) — no proof forgery or validator/relayer collusion is required, only submission of a legitimately failed but still verifiable event.

### Recommendation
Add an explicit gate on `receipt.success` in `process_delivery_receipt` before registering the reward — e.g. only pay out the fee when `receipt.success == true`, and either return an error, emit a distinct `MessageDeliveryFailed`/`MessageDeliveryFailedButProcessed` event, or apply a different (e.g. reduced/no) reward policy when `success == false`, while still removing the stale `PendingOrder` to avoid leaving it dangling.

### Proof of Concept
1. A message is queued via `do_process_message`, creating a `PendingOrder { nonce, fee, .. }` (see lines 426-436 of `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`).
2. The message is relayed to Ethereum, but the dispatched command reverts on-chain (e.g., due to a downstream contract-level failure), so the Gateway emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. Any account acts as relayer and calls `submit_delivery_receipt` with a valid proof of this (legitimately failed) event.
4. `T::Verifier::verify` succeeds (the log genuinely exists and is included), `DeliveryReceipt::try_from` decodes `success: false` correctly.
5. `process_delivery_receipt` checks only `gateway` match and `PendingOrders` existence — both pass — then unconditionally calls `T::RewardPayment::register_reward(&reward_account, ..., order.fee)` and removes the order, paying the relayer despite the failed dispatch, as shown at [7](#0-6) .

### Citations

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/delivery_receipt.rs (L10-12)
```rust
sol! {
	event InboundMessageDispatched(uint64 indexed nonce, bytes32 topic, bool success, bytes32 reward_address);
}
```

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

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound.rs (L407-418)
```rust
		let relayer = BridgeHubWestendSender::get();
		let reward_account = AssetHubWestendReceiver::get();
		let receipt = DeliveryReceipt {
			gateway: EthereumGatewayAddress::get(),
			nonce: 1,
			reward_address: reward_account.into(),
			topic: H256::zero(),
			success: true,
		};

		// Submit a delivery receipt
		assert_ok!(EthereumOutboundQueueV2::process_delivery_receipt(relayer, receipt));
```
