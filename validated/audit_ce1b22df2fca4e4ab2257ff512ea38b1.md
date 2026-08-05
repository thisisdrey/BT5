Audit Report

## Title
`EthereumOutboundQueueV2::process_delivery_receipt` pays relayer reward and settles the order regardless of the Ethereum-side dispatch `success` flag - (File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs)

## Summary
`DeliveryReceipt` decoded from the Ethereum `InboundMessageDispatched` event carries a `success: bool` field indicating whether the message actually executed correctly on Ethereum <cite repo="ThankGodontt/polkadot-sdk--016" path="bridges/snowbridge/primitives/outbound-queue/src/v2/delivery_receipt.rs" start="10-27" end="27" />. `Pallet::process_delivery_receipt` never reads or branches on `receipt.success`; it unconditionally pays `order.fee` to the reward account and removes the `PendingOrder` as long as the fee is nonzero and the gateway/nonce checks pass [1](#0-0) .

## Finding Description
`process_delivery_receipt` verifies `receipt.gateway` matches `T::GatewayAddress`, resolves the reward account, fetches the `PendingOrder` by `receipt.nonce`, and — if `order.fee > 0` — calls `T::RewardPayment::register_reward` before unconditionally calling `<PendingOrders<T>>::remove(nonce)` and emitting `Event::MessageDelivered` [2](#0-1) . Nowhere in this function, or anywhere else in the `outbound-queue-v2` pallet, is `receipt.success` inspected — a repo-scoped search for `success` within `bridges/snowbridge/pallets/outbound-queue-v2/` returns no matches outside the primitive struct definition itself. The `DeliveryReceipt::try_from<&Log>` decode path faithfully carries the Ethereum-emitted `success` value into the struct [3](#0-2) , but that value is discarded once it reaches pallet logic. The only guards present (`GatewayAddress` match and `PendingOrders` existence via nonce) attest that a message with this nonce was emitted and this is the correct gateway — they say nothing about whether the dispatch on Ethereum actually succeeded, so they are insufficient to prevent payout on failure.

## Impact Explanation
This allows a relayer/reward account to receive the full `order.fee` reward for message deliveries where the underlying Ethereum-side command dispatch failed (`success == false`), because payout and irreversible removal of `PendingOrders[nonce]` both proceed identically regardless of that flag. This violates the settlement invariant that payout state must only advance after dispatch and execution actually succeed, and results in unbacked/misallocated payout of the exact value `order.fee`, permanently losing the ability to re-process or correctly reward that nonce since the order is removed unconditionally.

## Likelihood Explanation
Any unprivileged relayer who observes a genuine `InboundMessageDispatched(nonce, topic, success=false, reward_address)` event (a real and expected occurrence, e.g. insufficient gas or reverting command execution on Ethereum) can submit the valid Merkle/verifier proof for that log via `submit_delivery_receipt`, and `process_delivery_receipt` will decode `success: false` but pay the reward exactly as if it were `true`. No privileged actor, compromised relayer, or off-chain trust assumption beyond normal message-proof submission is required.

## Recommendation
Branch on `receipt.success` in `process_delivery_receipt`: only invoke `T::RewardPayment::register_reward` and remove the `PendingOrder` when `receipt.success == true`. On `false`, either withhold reward and remove the order as "failed", or preserve/requeue it for retry rather than treating it identically to a successful delivery, and emit a distinct event (e.g., `MessageDeliveryFailed`) instead of `MessageDelivered`.

## Proof of Concept
1. A message with nonce `N`, fee `F > 0` is committed and stored in `PendingOrders<T>` via `do_process_message` [4](#0-3) .
2. On Ethereum, the command dispatch for nonce `N` fails, and the Gateway emits `InboundMessageDispatched(nonce=N, topic, success=false, reward_address=R)`.
3. A relayer calls `submit_delivery_receipt` with a valid Merkle/verifier proof for this log; it decodes into `DeliveryReceipt{ nonce: N, success: false, reward_address: R, .. }` via `TryFrom<&Log>` [5](#0-4)  and invokes `process_delivery_receipt`.
4. `process_delivery_receipt` finds `order.fee = F > 0`, calls `T::RewardPayment::register_reward(&R, .., F)`, and removes `PendingOrders[N]` — identical to the success path — because `receipt.success` is never checked [6](#0-5) .
5. `R` is paid `F` and `Event::MessageDelivered { nonce: N }` is emitted despite the dispatch having failed on Ethereum — a unit/integration test asserting reward balance and `PendingOrders` state before/after calling `process_delivery_receipt` with `success: false` reproduces this directly.

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L426-436)
```rust
			// Generate `PendingOrder` with fee attached in the message, stored
			// into the `PendingOrders` map storage, with assigned nonce as the key.
			// When the message is processed on ethereum side, the relayer will send the nonce
			// back with delivery proof, only after that the order can
			// be resolved and the fee will be rewarded to the relayer.
			let order = PendingOrder {
				nonce,
				fee,
				block_number: frame_system::Pallet::<T>::current_block_number(),
			};
			<PendingOrders<T>>::insert(nonce, order);
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
