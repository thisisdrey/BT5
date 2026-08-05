The claim is confirmed accurate against the current code. `process_delivery_receipt` reads `receipt.gateway`, `receipt.reward_address`, and `receipt.nonce` but never checks `receipt.success` before calling `T::RewardPayment::register_reward` and removing the `PendingOrder` entry, exactly as described in the finding.Audit Report

## Title
Unconditional relayer reward payout regardless of `DeliveryReceipt.success` in `EthereumOutboundQueueV2::process_delivery_receipt` - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

## Summary
`Pallet::process_delivery_receipt` pays the relayer reward via `T::RewardPayment::register_reward` and permanently removes the corresponding `PendingOrders` entry without ever inspecting `receipt.success`, even though `DeliveryReceipt::success` is decoded directly from the Ethereum `InboundMessageDispatched` event log and reflects whether the bridged message actually executed successfully on the Gateway contract. This means a relayer can obtain payment and finalize settlement for a nonce whose execution reverted on Ethereum, exactly as if it had succeeded.

## Finding Description
`submit_delivery_receipt` verifies the authenticity of the event log via `T::Verifier::verify`, then decodes it into a `DeliveryReceipt` struct via `DeliveryReceipt::try_from`, which populates `success` straight from the Solidity event's `success` field (`InboundMessageDispatched(uint64 indexed nonce, bytes32 topic, bool success, bytes32 reward_address)`) with no additional gating: [1](#0-0) 

This receipt is passed to `process_delivery_receipt`, which checks the gateway address, resolves the reward account, looks up the `PendingOrder`, and then unconditionally pays `order.fee` and removes the `PendingOrders` entry — `receipt.success` is never read anywhere in this function: [2](#0-1) 

`T::Verifier::verify` only authenticates that the log genuinely originates from the known Gateway contract and was included in a proven Ethereum block/receipt trie — it says nothing about the semantic execution outcome encoded in the `success` boolean. Consequently, a real, honestly-observed but reverted execution on Ethereum (which still emits the `InboundMessageDispatched` log with `success = false`) produces a perfectly valid, provable event that passes every existing check in `submit_delivery_receipt` and `process_delivery_receipt`.

## Impact Explanation
This breaks the required invariant that "receipts and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically." Reward payout (`register_reward`) and final settlement (`PendingOrders::remove(nonce)`) both occur regardless of whether the underlying message actually executed successfully on Ethereum. Once `PendingOrders::remove(nonce)` executes, there is no path to revisit or contest that nonce, so a reverted execution is treated identically to a successful one, misallocating protocol-controlled reward funds to relayers for operations that never completed. This matches the impact gate's "duplicate settlement or payout" / broken payout-correctness category.

## Likelihood Explanation
Exploitation requires no privileged role, no validator/relayer collusion, and no key compromise: any relayer can call the public `submit_delivery_receipt` extrinsic with a legitimate proof for any mined transaction on the Gateway contract, including ones whose command execution reverted (still emitting a valid, provable log with `success = false`). This is fully attacker-reachable via ordinary public extrinsic submission using genuine on-chain data.

## Recommendation
In `process_delivery_receipt`, branch on `receipt.success`: only invoke `T::RewardPayment::register_reward` when `receipt.success == true`. For the `false` case, define explicit failure semantics (e.g., no reward, a distinct `MessageDeliveryFailed` event, or retry/refund logic) before removing the `PendingOrder`, so payout and final settlement are gated on confirmed successful execution rather than merely on proof authenticity and decodability.

## Proof of Concept
1. A message is enqueued via `do_process_message`, inserting `PendingOrders[nonce] = PendingOrder { fee, nonce, block_number }` (bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs:426-436).
2. The message is relayed to the Ethereum Gateway; its command execution reverts on-chain, but the Gateway still emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. A relayer obtains this genuine log plus a valid proof and calls `submit_delivery_receipt(origin, event)`. `T::Verifier::verify` succeeds (log is authentic); `DeliveryReceipt::try_from` decodes `success: false` (bridges/snowbridge/primitives/outbound-queue/src/v2/delivery_receipt.rs:38-51).
4. `process_delivery_receipt` never reads `receipt.success`; it pays `order.fee` via `register_reward` and calls `PendingOrders::remove(nonce)` (bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs:464-477) — identical behavior to the `success: true` case, finalizing payout for a failed execution. A unit test asserting `assert_noop!`/balance-unchanged when `success: false` versus the current unconditional payout would demonstrate the gap.

### Citations

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
