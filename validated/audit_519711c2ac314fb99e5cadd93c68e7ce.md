### Title
`EthereumOutboundQueueV2::process_delivery_receipt` pays relayer reward and settles the order regardless of the Ethereum-side dispatch `success` flag - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
The external report's core defect is a code path that skips required accounting logic depending on an input field, resulting in a wrong value being finalized regardless of whether the underlying operation actually took the expected effect. The Snowbridge Outbound Queue V2 analog is `Pallet::process_delivery_receipt`, which decodes a `DeliveryReceipt` (including a `success: bool` field taken directly from the Ethereum `InboundMessageDispatched` event) but never inspects `receipt.success` before paying the relayer reward and irreversibly removing the `PendingOrder`.

### Finding Description
`DeliveryReceipt` is decoded from an Ethereum log and explicitly carries a `success` field describing whether the message dispatch on Ethereum succeeded: [1](#0-0) 

`process_delivery_receipt` verifies the gateway address and the receipt's Merkle/verifier proof (via the caller path through `submit_delivery_receipt`), looks up the `PendingOrder` by nonce, and pays out `order.fee` to the relayer/reward account — but the decoded `receipt.success` value is never read or branched on anywhere in this function: [2](#0-1) 

A repo-wide search confirms `receipt.success` (or any `.success` field read) is referenced nowhere in the pallet logic outside the struct definition itself — it is decoded from the Ethereum event but silently discarded. The reward payment and `PendingOrders::remove(nonce)` are unconditional on `order.fee > 0` and on successful proof verification of *that a message with this nonce was emitted*, not on whether the message actually executed successfully on Ethereum.

This is a direct structural analog to `_onDecreaseLever`: a field that is present specifically to gate whether an accounting branch should be treated as successful (`success` here, `auxAction.args.length` there) is ignored, so the settlement/payout state is finalized unconditionally instead of reflecting the true outcome.

### Impact Explanation
Because settlement (`PendingOrders::remove`) and reward payout (`T::RewardPayment::register_reward`) both proceed whenever a validly-proven `InboundMessageDispatched` event is presented, a relayer is paid the full fee even for message deliveries that failed on the Ethereum side (`success == false`). This breaks the "message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically" invariant: the bridge marks the order as delivered and pays out despite the underlying dispatch having failed, permanently losing the ability to re-process or re-reward that message and misallocating the relayer reward pot's funds.

### Likelihood Explanation
Any unprivileged relayer who observes (or can trigger) a failed `InboundMessageDispatched` event with `success = false` on the Gateway contract can submit the corresponding valid Merkle/verifier proof through `submit_delivery_receipt` and receive full payment as though the message succeeded — no privileged actor, malicious validator, or off-chain trust assumption is required, only a genuine but failed Ethereum-side execution, which is a realistic and expected occurrence in the bridge design (hence the explicit `success` field).

### Recommendation
Branch on `receipt.success` in `process_delivery_receipt`: only pay `order.fee` to the reward account when `receipt.success == true`; when `false`, either withhold/slash the reward, or keep/resubmit the order for a later successful delivery attempt instead of unconditionally removing it from `PendingOrders`.

### Proof of Concept
1. An `OutboundMessage` with nonce `N` and fee `F` is committed and stored in `PendingOrders`.
2. The message dispatch fails on Ethereum (e.g., insufficient gas at execution or a reverting command), and the Gateway contract emits `InboundMessageDispatched(nonce=N, topic, success=false, reward_address=R)`. [3](#0-2) 
3. A relayer proves this event via `submit_delivery_receipt`, which decodes it into a `DeliveryReceipt{ success: false, .. }` and calls `process_delivery_receipt`.
4. `process_delivery_receipt` finds `order.fee = F > 0`, calls `T::RewardPayment::register_reward(&reward_account, .., F)`, and removes the order — exactly as in the "success" path, since `receipt.success` is never checked: [4](#0-3) 
5. The relayer/reward account is paid `F` even though the message never executed correctly on Ethereum, and `MessageDelivered { nonce }` is emitted misleadingly.

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

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/delivery_receipt.rs (L35-51)
```rust
impl TryFrom<&Log> for DeliveryReceipt {
	type Error = DeliveryReceiptDecodeError;

	fn try_from(log: &Log) -> Result<Self, Self::Error> {
		let topics: Vec<B256> = log.topics.iter().map(|x| B256::from_slice(x.as_ref())).collect();

		let event = InboundMessageDispatched::decode_raw_log_validate(topics, &log.data)
			.map_err(|_| DeliveryReceiptDecodeError::DecodeLogFailed)?;

		Ok(Self {
			gateway: log.address,
			nonce: event.nonce,
			topic: H256::from_slice(event.topic.as_ref()),
			success: event.success,
			reward_address: event.reward_address.0,
		})
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
