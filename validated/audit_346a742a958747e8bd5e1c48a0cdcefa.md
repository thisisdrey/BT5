Confirmed. The code exactly matches the claim: `process_delivery_receipt` decodes `receipt.success` via `DeliveryReceipt::try_from` but never reads that field before paying the reward — the only gate is `order.fee > 0` [1](#0-0) , and the `success` flag is decoded straight from the Ethereum `InboundMessageDispatched` event in `DeliveryReceipt::try_from` <cite repo="Loderfordw/polkadot-sdk--027" path="bridges/snowbridge/primitives/outbound-queue/src/v2/delivery_receipt.rs" start="10-27" end="35-51" />. `submit_delivery_receipt` verifies the proof and forwards the decoded receipt directly to `process_delivery_receipt` without any success check [2](#0-1) .

Audit Report

## Title
`process_delivery_receipt` pays relayer rewards without checking the Ethereum-side `success` flag, allowing reward payout for failed message execution - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

## Summary
`DeliveryReceipt::success`, decoded from the Ethereum `InboundMessageDispatched` event, is never consulted in `process_delivery_receipt` before paying the relayer reward. The function only checks `order.fee > 0`, so a delivery receipt reporting execution failure (`success == false`) still results in a full reward payout, letting relayers collect rewards for messages that failed on Ethereum.

## Finding Description
`bridges/snowbridge/primitives/outbound-queue/src/v2/delivery_receipt.rs` decodes the Solidity event `InboundMessageDispatched(uint64 indexed nonce, bytes32 topic, bool success, bytes32 reward_address)` into a `DeliveryReceipt` struct carrying `success: bool`. `submit_delivery_receipt` verifies the Ethereum receipt proof via `T::Verifier::verify`, decodes the envelope into `DeliveryReceipt`, and forwards it unmodified to `process_delivery_receipt`. That function looks up `PendingOrder` by `nonce` and pays the reward whenever `order.fee > 0` via `T::RewardPayment::register_reward`, never reading `receipt.success`. The gate that should reflect "did the message actually execute successfully on Ethereum" is entirely disconnected from the real outcome — it only checks that a fee was attached at submission time, which is always true for any order created with `fee > 0` regardless of later execution outcome.

## Impact Explanation
Any relayer can submit a cryptographically valid proof for a genuinely failed `InboundMessageDispatched(success=false)` event and still receive the full `order.fee` reward via `T::RewardPayment::register_reward`. This is a duplicate/unbacked payout: value is disbursed for work (successful message execution) that was not actually delivered, draining the bridge reward pool for messages that reverted or otherwise failed on Ethereum. This matches "theft or unbacked mint or unlock" / "duplicate settlement or payout" in the impact gate, since the reward is settled on the wrong condition (delivery attempt rather than successful delivery).

## Likelihood Explanation
No privileged actor or malicious peer assumption is required. `submit_delivery_receipt` is a plain signed extrinsic open to any account, and a legitimately failed delivery (e.g., reverted command execution, gas misestimation) on Ethereum naturally produces a real, verifiable `success=false` event log. A normal relayer submitting a receipt for such a failure triggers the erroneous payout under standard usage — no forgery or special conditions are needed beyond the message failing on the Ethereum side, which is a realistic and expected occurrence.

## Recommendation
Gate `T::RewardPayment::register_reward` on `receipt.success == true`. For `success == false`, withhold the reward (or apply a distinct reduced-compensation path) while still removing the `PendingOrder` from storage to avoid stale state, and emit a distinguishing event (e.g., `MessageDeliveryFailed`) so failed deliveries are observable and reconciled correctly on-chain.

## Proof of Concept
1. Submit a message via `do_process_message`, creating a `PendingOrder { nonce, fee: 1_000_000, .. }` [3](#0-2) .
2. On Ethereum, message execution reverts; the Gateway still emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. Relayer builds a valid `EventProof` for this log (following the pattern in `bridges/snowbridge/pallets/outbound-queue-v2/src/test.rs`) with `success` encoded as `false`, and calls `submit_delivery_receipt`.
4. `T::Verifier::verify` succeeds, `DeliveryReceipt::try_from` decodes `success = false` correctly, but `process_delivery_receipt` still calls `T::RewardPayment::register_reward(&reward_account, .., order.fee)` since it only checks `order.fee > 0`.
5. Assert `submit_delivery_receipt` returns `Ok` and `Event::RewardRegistered`/`Event::MessageDelivered` fires with the full fee reward despite `success == false`, confirming the missing check.

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
