### Title
`submit_delivery_receipt` pays relayer reward without checking Ethereum-side delivery `success` flag - ([File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs])

### Summary
`Pallet::process_delivery_receipt` in the Snowbridge outbound-queue-v2 pallet pays the relayer's fee reward and settles the `PendingOrders` entry based solely on `order.fee > 0`, without ever inspecting the `success` field decoded from the Ethereum `InboundMessageDispatched` event. This mirrors the external report's core broken invariant: a semi-trusted actor (here, the relayer) controls part of the "external call" outcome (execution of the message on Ethereum) yet the local settlement/reward logic trusts a proxy value (mere presence of a verified receipt + non-zero fee) instead of verifying that the actual work was completed successfully, letting the actor extract payout disproportionate to the value delivered.

### Finding Description
The `DeliveryReceipt` struct decoded from the Ethereum Gateway's `InboundMessageDispatched` event explicitly carries a `success: bool` field: [1](#0-0) 

However, `process_delivery_receipt` never reads or checks this field before paying the reward: [2](#0-1) 

The flow is:
1. `submit_delivery_receipt` verifies the receipt's cryptographic proof (`T::Verifier::verify`) and decodes it into a `DeliveryReceipt`. [3](#0-2) 
2. `process_delivery_receipt` looks up the `PendingOrder` by `nonce`, and if `order.fee > 0`, unconditionally calls `T::RewardPayment::register_reward(&reward_account, ..., order.fee)`, then removes the order.
3. At no point is `receipt.success` consulted. The gate for reward eligibility is only "does a valid proof exist for this nonce", not "did the message actually execute successfully on Ethereum".

This is analogous to the external report's Issue: the periphery manager could manipulate outcome-adjacent state (swap return value, backing-assets value via reentrant deposit) while defensive checks (measure 1/2) failed to bind the payout strictly to the intended successful outcome. Here, the relayer (an unprivileged, permissionless actor who calls `submit_delivery_receipt`) similarly controls one leg of the process — nothing prevents them from delivering a message to Ethereum in a way that reverts/fails dispatch (e.g., malformed command execution, insufficient gas budgeted on their own transaction, or deliberately choosing a message whose commands fail) and then submitting the failure receipt (still cryptographically valid, since the Ethereum Gateway emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)` regardless of dispatch success) to collect the fee anyway.

### Impact Explanation
This breaks the intended settlement invariant that "message queues... and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically." Rewards are meant to compensate for successful relaying of governance/system/asset-transfer commands from BridgeHub to Ethereum; paying out for failed executions is an unbacked/underpriced-work payout: the relayer is compensated fee-for-fee regardless of whether the bridged operation (e.g., an XCM-originated asset transfer command) actually completed, degrading the economic assumption that reward == successful delivery. Over many failed-but-relayed messages, this allows systematic drain of the fee pot without providing the corresponding service, and removes any economic disincentive for relayers to submit messages designed to fail (e.g., malicious gas griefing on the Ethereum side) while still collecting Substrate-side rewards.

### Likelihood Explanation
The `submit_delivery_receipt` extrinsic is a public, permissionless entry point (`ensure_signed(origin)?` only) reachable by any relayer holding a valid Ethereum-side transaction receipt/proof. No governance or privileged relationship is required — an ordinary relayer who observes their own dispatch fail on Ethereum (whether accidentally or by design) can still submit that receipt and be paid, since `success` is decoded but discarded. This requires no malicious peer/validator/prover assumption; it is purely a local logic gap in `process_delivery_receipt`.

### Recommendation
Gate the reward payment on `receipt.success`. If `success == false`, either withhold the reward entirely, or apply a materially reduced/no-fee path, while still removing the `PendingOrder` (to prevent it from being resubmitted) and emitting an event that distinguishes failed delivery from successful delivery (e.g. a separate `MessageDeliveryFailed` event vs. `MessageDelivered`). This aligns the payout state machine with the "decode, dispatch, execution, and settlement succeed atomically" requirement.

### Proof of Concept
1. A relayer submits an outbound message via the normal flow; `PendingOrders[nonce]` is created with a non-zero `fee`.
2. The relayer executes the corresponding `InboundMessageDispatched`-emitting transaction on Ethereum, but engineers/observes a failure of the inner command dispatch (Gateway contract still emits the event with `success = false`, since the event is emitted regardless of per-command failure semantics of the Gateway).
3. The relayer calls `submit_delivery_receipt` with a valid proof of that event log.
4. `T::Verifier::verify` succeeds (the proof is valid — it truthfully proves a `success = false` event occurred).
5. `process_delivery_receipt` reaches `if order.fee > 0 { T::RewardPayment::register_reward(...) }` — this check does not read `receipt.success`, so the reward is registered exactly as if delivery had succeeded, confirmed by: [4](#0-3) 
6. The order is removed and `MessageDelivered` is emitted, even though the underlying bridged operation failed on Ethereum.

Note: I could not fully verify from this index whether Ethereum-side Solidity Gateway contract code (out of scope of this repo) always emits `success=false` for command-level failures versus reverting the whole transaction (which would prevent the event from being emitted at all). This uncertainty affects real-world exploitability, but the Substrate-side logic in this repository unconditionally trusts the receipt's fee-eligibility without consulting the `success` field it explicitly decodes, which is the concrete local code defect being reported.

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
