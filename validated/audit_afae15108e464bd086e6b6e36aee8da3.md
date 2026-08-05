Confirmed: `process_delivery_receipt` in `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs` never checks `receipt.success` before paying the relayer reward and removing the `PendingOrder`. The `DeliveryReceipt` decodes `success` from the on-chain `InboundMessageDispatched` event log, but the field is unused in the reward-payment path, matching the claim exactly.

Audit Report

## Title
`process_delivery_receipt` ignores `receipt.success` and pays relayer reward for failed Ethereum executions - (File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs)

## Summary
`process_delivery_receipt` verifies the gateway address and pending nonce, then unconditionally pays the relayer's fee via `T::RewardPayment::register_reward` and removes the `PendingOrder`, without ever inspecting `receipt.success`. The `success` field is decoded from the genuine `InboundMessageDispatched` Ethereum event but is dropped on the floor by the pallet logic, so failed/reverted executions are rewarded exactly like successful ones.

## Finding Description
`submit_delivery_receipt` [1](#0-0)  verifies the event log proof via `T::Verifier::verify`, decodes it into a `DeliveryReceipt` via `DeliveryReceipt::try_from`, and forwards to `process_delivery_receipt`. `DeliveryReceipt::try_from` [2](#0-1)  decodes `event.success` from the `InboundMessageDispatched` Solidity event and stores it on the struct, confirming the value is authenticated and available.

`process_delivery_receipt` [3](#0-2)  then: checks `receipt.gateway == T::GatewayAddress::get()`, derives `reward_account`, loads `PendingOrders<T>::get(nonce)`, and if `order.fee > 0` calls `T::RewardPayment::register_reward(&reward_account, T::DefaultRewardKind::get(), order.fee)` — with no `ensure!(receipt.success, ...)` guard anywhere in the function. The order is then removed and `MessageDelivered` is emitted regardless of `success`.

Existing guards (`InvalidGateway`, `InvalidPendingNonce`) only bind the receipt to the correct contract and nonce; none of them validate delivery outcome. Since `success` is part of the verified log content, no additional proof step is required to enforce it — only a missing conditional.

## Impact Explanation
This is an unbacked/unauthorized payout: the relayer's `order.fee` is paid from bridge-controlled reward funds for a message that was never successfully executed on Ethereum, and the `PendingOrder` is removed, permanently foreclosing any legitimate re-processing or accounting for that nonce. This matches the "theft or unbacked mint or unlock" / "duplicate settlement or payout" impact category, since any signed account can trigger `submit_delivery_receipt` once a genuine (but failure-indicating) event proof exists, extracting fee value not earned by successful bridge work.

## Likelihood Explanation
High feasibility: the attacker only needs a legitimately emitted `InboundMessageDispatched` log where `success=false` (which occurs whenever the destination-side dispatch reverts, e.g., due to an out-of-gas condition or command failure unrelated to attacker control) and a standard Merkle/receipt proof for it — both of which are part of normal bridge operation, not exploitation of the verifier. `submit_delivery_receipt` is a public extrinsic open to any signed origin, and the exploit requires no privileged role, collusion, or off-chain compromise, so it is directly reachable and repeatable for every failed delivery.

## Recommendation
Add `ensure!(receipt.success, Error::<T>::DeliveryFailed);` near the top of `process_delivery_receipt` (after the gateway check, before reward computation), introducing a new `DeliveryFailed` variant in `Error<T>`. If failed deliveries must still clear `PendingOrders` to prevent indefinite retries/replay, separate the "clear order" logic from the "pay fee" logic so removal still occurs but `register_reward` is skipped when `success == false`.

## Proof of Concept
1. A message with `fee = 1_000_000` is enqueued, producing `PendingOrders[nonce] = PendingOrder { nonce, fee: 1_000_000, .. }` via `do_process_message`.
2. The message is relayed to Ethereum, but its command execution reverts on-chain; the Gateway contract emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. Any signed account submits `submit_delivery_receipt(origin, event)` with a valid proof for that log.
4. `T::Verifier::verify` succeeds (log is authentic); `DeliveryReceipt::try_from` decodes `success=false`.
5. `process_delivery_receipt` passes the gateway check, finds `order.fee > 0`, and calls `register_reward(&reward_account, kind, 1_000_000)` — paying the fee despite `success == false` — then removes `PendingOrders[nonce]` and emits `MessageDelivered { nonce }`.
6. A regression test asserting `register_reward` is NOT invoked (or errors) when constructing a `DeliveryReceipt` with `success: false` would fail against current code, confirming the vulnerability.

### Citations

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
