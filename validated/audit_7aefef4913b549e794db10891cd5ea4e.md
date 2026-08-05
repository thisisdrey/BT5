The code confirms the claim exactly as described. `DeliveryReceipt::success` is decoded from the Ethereum `InboundMessageDispatched` event but is never inspected in `process_delivery_receipt`, which unconditionally pays the relayer reward and removes the `PendingOrder`.There are zero occurrences of `receipt.success` anywhere in the codebase outside its struct definition — confirming the field is decoded but genuinely dead/unused in the settlement logic.Audit Report

## Title
`submit_delivery_receipt` pays relayer reward and clears `PendingOrder` without checking `DeliveryReceipt.success`, settling failed Ethereum executions as if they succeeded - (File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs)

## Summary
`DeliveryReceipt` decodes the `success` flag from Ethereum's `InboundMessageDispatched(nonce, topic, success, reward_address)` event, but `process_delivery_receipt` never reads or branches on `receipt.success` before paying the relayer's full `order.fee` reward and permanently removing the `PendingOrder`. A grep across the entire repository confirms `receipt.success` is referenced nowhere outside the struct definition itself, meaning the field is decoded and then silently discarded in production logic.

## Finding Description
`DeliveryReceipt` is decoded from the Gateway's log with a `success: bool` field intact: <cite repo="Lauraivanka/polkadot-sdk--034" path="bridges/snowbridge/primitives/outbound-queue/src/v2/delivery_receipt.rs" start="10-27" /> `submit_delivery_receipt` verifies the proof, decodes the receipt, and immediately forwards it to `process_delivery_receipt` without any success check in between: [1](#0-0)  `process_delivery_receipt` itself only validates the gateway address and the existence of the `PendingOrder`, then unconditionally registers the reward and removes the order: [2](#0-1)  There is no code path anywhere in the pallet, its tests, or its integration tests that reads `receipt.success` to gate reward payment or to emit a distinct failure event — confirmed by a repository-wide search returning zero matches for `receipt.success`. Existing integration tests (`send_weth_from_asset_hub_to_ethereum`, etc.) only exercise the `success: true` path, and the `AssetsTrapped`/`claim_assets` refund mechanism that exists for the *inbound* queue (Ethereum → AssetHub, on invalid XCM) has no counterpart on this *outbound* queue side for assets already withdrawn on AssetHub via `WithdrawAsset`/`ReserveAssetDeposited` in the originating `InitiateTransfer`. Once a message is queued with a `PendingOrder`, the only exit from that state is `submit_delivery_receipt`, and that exit always treats the message as fully settled regardless of the encoded execution outcome.

## Impact Explanation
This satisfies the "message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically" pivot: settlement (`PendingOrders::remove` + `RewardPayment::register_reward`) advances even when the receipt itself encodes `success = false`. The result is (a) relayers get paid the full fee for deliveries whose destination-side commands (unlock/mint/`Transact`) reverted on Ethereum, an underpriced/incorrect settlement paid from real fee funds, and (b) the `PendingOrder` is destroyed with `MessageDelivered` emitted, permanently foreclosing any way to detect the failure or trigger a refund of the assets that were withdrawn/reserved on AssetHub/BridgeHub when the message was originally queued — a permanent user-fund lock with no recourse.

## Likelihood Explanation
The path is reachable by any unprivileged, honest relayer simply following the documented flow: submit a valid proof for whatever `InboundMessageDispatched` event the Gateway actually emitted. Genuine execution failures on Ethereum (insufficient gas budgeted by `GasMeter`, a reverting mint/unlock call, or a rejecting receiving contract) naturally produce `success = false` without any adversarial relayer, prover, or governance action — making this a high-likelihood, easily triggered scenario under normal bridge operation rather than an edge case.

## Recommendation
In `process_delivery_receipt`, branch on `receipt.success`: on `true`, keep current behavior; on `false`, withhold or reduce the relayer reward, emit a distinct failure event (e.g. `MessageDeliveryFailed`) carrying `nonce`/`topic`, and provide a mechanism to refund/unlock the assets originally withdrawn or reserved on AssetHub/BridgeHub back to the sender or a claimable location, analogous to the existing inbound-side `AssetsTrapped`/`claim_assets` recovery flow.

## Proof of Concept
1. User executes `InitiateTransfer` on AssetHub sending WETH to Ethereum; `WithdrawAsset`/`ReserveAssetDeposited` withdraws the user's assets, and BridgeHub's outbound queue creates `PendingOrder{nonce, fee}`.
2. On Ethereum, the Gateway's command execution (e.g., `Transact`/mint) reverts, so the Gateway emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. A relayer submits this event with a valid Beefy/verifier proof via `submit_delivery_receipt`; `T::Verifier::verify` succeeds and `DeliveryReceipt::try_from` decodes `success = false`.
4. `process_delivery_receipt` executes: [3](#0-2)  paying the relayer the full `order.fee` and removing the `PendingOrder`/emitting `MessageDelivered`, identically to the success case, even though the destination-side asset movement never occurred and no refund mechanism exists for the assets already withdrawn on AssetHub.

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
