I've confirmed the code matches the claim exactly. The `success` field is decoded from the Ethereum event but never checked in `process_delivery_receipt`.

Audit Report

## Title
`snowbridge-pallet-outbound-queue-v2::process_delivery_receipt` ignores `DeliveryReceipt.success`, rewarding relayers and closing pending orders for reverted Ethereum command execution - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

## Summary
The `DeliveryReceipt` struct decodes an explicit `success: bool` field from the Ethereum `InboundMessageDispatched` event, indicating whether the Gateway contract actually executed the outbound commands successfully. `process_delivery_receipt` never reads `receipt.success`, unconditionally paying the relayer reward and removing the `PendingOrders` entry regardless of whether Ethereum-side execution succeeded or reverted.

## Finding Description
`DeliveryReceipt` decodes the `success` field directly from the verified Ethereum log via `TryFrom<&Log>`, storing it as `success: event.success` [1](#0-0) . The extrinsic `submit_delivery_receipt` verifies the proof, decodes the receipt, and forwards it to `process_delivery_receipt` without ever consulting `success` itself [2](#0-1) . Inside `process_delivery_receipt`, the only checks performed are on `receipt.gateway` and the existence of a `PendingOrders` entry for `receipt.nonce`; the function then unconditionally calls `T::RewardPayment::register_reward` for `order.fee` and removes the `PendingOrders` entry, emitting `Event::MessageDelivered` [3](#0-2) . There is no branch anywhere in this path that inspects `receipt.success`, so a legitimately-proven event reporting `success: false` (an Ethereum-side revert of the dispatched command) is processed identically to `success: true`.

## Impact Explanation
This causes duplicate/incorrect settlement of relayer rewards: fee payment for delivery work whose remote execution did not actually complete, and permanent removal of `PendingOrders` state for a message whose corresponding command never took effect on Ethereum, with no other pallet mechanism to retry or flag the failure. This matches the "duplicate settlement or payout" and "runtime bugs that compromise intended behavior" categories in the impact gate, since a receipt/payout record advances to a final "delivered" state without the execution-success invariant being enforced. The `success` field's decoding is bound to a genuinely verified proof (not forgeable), but the pallet's handling of that verified data is itself incorrect.

## Likelihood Explanation
The `success` field being `false` is not attacker-crafted forgery — it originates from real Ethereum-side execution failures (e.g., out-of-gas dispatch of a command in the Gateway contract), which is a normal and expected occurrence in production bridge operation, not a contrived edge case. Any relayer submitting a legitimate delivery-receipt proof for a message whose remote execution failed will trigger this path unconditionally, making the likelihood high whenever any outbound command execution fails on the Ethereum side.

## Recommendation
In `process_delivery_receipt`, branch on `receipt.success`: if `true`, proceed as today; if `false`, withhold or reduce the relayer reward, emit a distinct event (e.g., `MessageDeliveryFailed { nonce }`), and explicitly decide whether the `PendingOrders` entry should be removed, retried, or retained for governance/manual handling.

## Proof of Concept
1. A message is queued via `do_process_message`, creating a `PendingOrder` for `nonce = N` with `fee = F` [4](#0-3) .
2. On Ethereum, the Gateway's dispatch of the outbound command for nonce `N` reverts, so the Gateway emits `InboundMessageDispatched(nonce=N, topic, success=false, reward_address)`.
3. A relayer builds a proof from this genuine event and calls `submit_delivery_receipt(origin, event)`; `T::Verifier::verify` succeeds and `DeliveryReceipt::try_from` decodes `success: false` correctly.
4. `process_delivery_receipt` checks `receipt.gateway` (matches) and `PendingOrders::get(N)` (present with `fee = F`), then unconditionally calls `T::RewardPayment::register_reward(&reward_account, ..., F)` and `PendingOrders::<T>::remove(N)`, emitting `Event::MessageDelivered { nonce: N }` — identical control flow to the `success: true` case demonstrated in the existing integration test fixture [5](#0-4) , proving the field is dead for control-flow purposes.

### Citations

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

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound.rs (L103-122)
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

		assert_expected_events!(
			BridgeHubWestend,
			vec![
				RuntimeEvent::BridgeRelayers(pallet_bridge_relayers::Event::RewardRegistered { .. }) => {},
			]
		);
	});
```
