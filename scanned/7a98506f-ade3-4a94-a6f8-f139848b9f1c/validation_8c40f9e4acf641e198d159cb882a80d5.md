I found a concrete local analog in the Snowbridge outbound-queue-v2's delivery-receipt / reward flow, where a "success" signal from an external actor is trusted without validating it against the actual message outcome, mirroring the report's core flaw: a failure/success signal is not tied to real, verified state before funds move.

### Title
Outbound-queue-v2 pays relayer reward on `submit_delivery_receipt` regardless of whether delivery actually succeeded - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
`process_delivery_receipt` in `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs` (lines 446-480) pays out the fee attached to a `PendingOrder` whenever a `DeliveryReceipt` with a matching nonce and gateway is submitted, and unconditionally removes the order afterwards. Unlike the ProofVerification-gated `submit` path in the inbound queue, this code path does not inspect `receipt.success` (a field present on `DeliveryReceipt`, as seen used in the integration test at `cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound.rs:794-800`) before rewarding — the reward is paid purely because the relayer produced a verifiable receipt for the nonce, not because the message was actually executed successfully on Ethereum.

### Finding Description
`process_delivery_receipt` does the following: [1](#0-0) 

```
pub fn process_delivery_receipt(...) -> DispatchResult {
    ensure!(T::GatewayAddress::get() == receipt.gateway, Error::<T>::InvalidGateway);
    let reward_account = if receipt.reward_address == [0u8; 32] { relayer } else { receipt.reward_address.into() };
    let nonce = receipt.nonce;
    let order = <PendingOrders<T>>::get(nonce).ok_or(Error::<T>::InvalidPendingNonce)?;
    if order.fee > 0 {
        T::RewardPayment::register_reward(&reward_account, T::DefaultRewardKind::get(), order.fee);
    }
    <PendingOrders<T>>::remove(nonce);
    Self::deposit_event(Event::MessageDelivered { nonce });
    Ok(())
}
```

`DeliveryReceipt` carries a `success: bool` field (used explicitly by the emulated test at `cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound.rs:794-800`), yet `process_delivery_receipt` never reads or checks it. As soon as a receipt matching an existing `PendingOrder` nonce and the configured gateway address is provided, the pending fee is unconditionally credited to `reward_account` and the order is removed. There is no re-verification that the underlying Ethereum execution actually succeeded (i.e., that `success == true`), and no path exists to re-attempt reward or refund logic once the order is deleted — the order is a one-shot, irreversible payout gate keyed only on nonce+gateway matching.

This mirrors the reported bug class precisely: a downstream "handle error case" branch (here, the absent branch for `success == false`) that is never actually inspected, so the pallet proceeds to settle value (the relayer reward) regardless of whether the operation it is rewarding actually succeeded. Where the message-queue pallet and asset-conversion pallet wrap their state mutations in `with_transaction`/`with_storage_layer` so that failures are provably rolled back, this pallet has no equivalent safety net because the "failure" signal (`receipt.success == false`) is a value inside an already-verified extrinsic payload rather than a `DispatchError` — so no storage-layer rollback mechanism applies to it at all.

### Impact Explanation
An unprivileged relayer (any signed account able to produce a receipt proof from Ethereum logs) can submit a `DeliveryReceipt` with `success: false` for a nonce whose Ethereum-side execution actually failed or reverted, and still receive the full relayer reward fee tied to that `PendingOrder`, because the pallet never branches on `success`. This causes public underpriced/unbacked payout: rewards are settled to relayers for message deliveries that did not actually succeed, draining the reward pool and misaligning the "useful and valid work" compensation model that Snowbridge and the sibling bridge-relayers pallet rely on (see `bridges/docs/running-relayer.md:37-40`, which explicitly states relayers should be compensated only for **valid** transactions).

### Likelihood Explanation
Likelihood is high in practice: the check is a straightforward oversight rather than requiring any privileged actor, malicious validator, or leaked key — any relayer with a legitimate, correctly-proved `EventProof`/receipt structure (which only proves the receipt log was included in Ethereum, not that the referenced action succeeded) can trigger the payout path deterministically every time a delivery is (or is not) actually completed on Ethereum.

### Recommendation
Add an explicit branch on `receipt.success` in `process_delivery_receipt`: only call `T::RewardPayment::register_reward` when `receipt.success == true`; on `false`, either re-queue the order for the actual on-chain command retried, or route it to a distinct "failed delivery" event/state that a governance or automated path can inspect, instead of unconditionally removing the `PendingOrder` and paying the fee.

### Proof of Concept
1. Send a message through `EthereumOutboundQueueV2::do_process_message`, producing a `PendingOrder { nonce, fee, .. }`.
2. Craft (or, in a permissioned test harness, directly call) `process_delivery_receipt` with `DeliveryReceipt { nonce, gateway: <configured gateway>, success: false, reward_address, .. }`.
3. Observe that `T::RewardPayment::register_reward` is still invoked with `order.fee`, and `PendingOrders::<T>::remove(nonce)` succeeds — identical to the `success: true` path exercised in `cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound.rs:792-810`, but with no differentiation for the failed-execution case. [2](#0-1) [3](#0-2)

### Citations

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

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound.rs (L792-812)
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
				RuntimeEvent::EthereumOutboundQueueV2(snowbridge_pallet_outbound_queue_v2::Event::MessageDelivered { .. }) => {},
			]
		);
	});
}
```
