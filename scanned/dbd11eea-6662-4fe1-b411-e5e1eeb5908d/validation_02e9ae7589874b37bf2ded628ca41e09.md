This confirms the vulnerability: `DeliveryReceipt::success` is decoded from the Ethereum event log but is **never checked** in `process_delivery_receipt`. Let me verify there's no other consumer of `success` and check how `commit`/reward flow relates to asset movement on the source chain to finalize the analog.### Title
`process_delivery_receipt` in Snowbridge outbound-queue-v2 ignores the on-chain `success` flag, rewarding relayers and clearing pending orders even when the Ethereum-side execution failed - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
The Snowbridge V2 outbound pipeline commits messages to Ethereum after assets have already been withdrawn/reserved on Asset Hub (via `InitiateTransfer`/`ReserveWithdraw` XCM instructions), and tracks per-message state in `PendingOrder`. Settlement of that pending state is driven entirely by `submit_delivery_receipt` → `process_delivery_receipt`, which decodes a `DeliveryReceipt` (including a `success: bool` field taken from the Ethereum `InboundMessageDispatched` event) but never inspects that flag before paying the relayer and removing the order. This mirrors the LiFi bug pattern: the bridging layer commits funds expecting successful execution on the far side, but the "did it actually succeed / should this be refunded" signal is dropped on the floor.

### Finding Description
The `DeliveryReceipt` type explicitly carries a `success` field decoded from the Ethereum `InboundMessageDispatched(nonce, topic, success, reward_address)` event: [1](#0-0) 

However, `Pallet::process_delivery_receipt` only checks `receipt.gateway`, resolves the reward account, fetches the `PendingOrder` by nonce, pays the relayer reward if `order.fee > 0`, and unconditionally removes the order and emits `MessageDelivered` — the decoded `receipt.success` value is never read or matched on: [2](#0-1) 

The extrinsic entry point simply verifies the log/proof and forwards the decoded receipt straight into this function: [3](#0-2) 

A grep across the pallet confirms `success` is not referenced anywhere in `outbound-queue-v2` logic — only decoded in the primitives crate and referenced in a single test, meaning production code has no branch for the failure case.

By the time this receipt is processed, the corresponding assets were already withdrawn from the user/AssetHub side via `WithdrawAsset`/`InitiateTransfer`/`ReserveWithdraw` XCM instructions on Asset Hub, e.g.: [4](#0-3) 

and the outbound queue's own documentation states the receipt is only supposed to finalize state "when the message has been verified and executed" — implicitly assuming execution succeeded: [5](#0-4) 

Since `success` is ignored, a message that reverted/failed to execute on the Ethereum Gateway contract (e.g., due to a swap/dispatch failure on the destination, analogous to LiFi's REFUNDED/received-wrong-token scenario) is treated identically to a successful delivery: the relayer is still rewarded from `order.fee`, and the `PendingOrder` is deleted, permanently closing out any bookkeeping trail that could otherwise trigger a refund of the assets that were reserved/withdrawn on Asset Hub. There is no other code path in this pallet (or its `AddTip`/`RewardLedger` integration) that re-examines `success` to unlock, refund, or reroute the corresponding source-side assets.

### Impact Explanation
This directly maps to the "duplicate settlement or payout" / "permanent user-fund or bridge-state lock" impact category: assets committed on Asset Hub for an Ethereum-bound transfer are irrevocably consumed regardless of whether the Ethereum-side dispatch actually succeeded, and the bridge's own success/failure signal for that dispatch is discarded before it can be acted upon. Any relayer can submit a valid delivery-receipt proof for a message that failed on Ethereum (the proof only attests to what the Ethereum event log actually says — `success: false` is just as provable as `success: true`) and still be rewarded, while the depositor whose assets were withdrawn on Asset Hub has no code path recovering them once `PendingOrders` entry is removed.

### Likelihood Explanation
This is reachable via the ordinary, unprivileged `submit_delivery_receipt` extrinsic once any relayer produces a valid Ethereum event-log proof — no malicious relayer/validator/governance action is required; any legitimate relayer submitting a receipt for a message whose dispatch legitimately failed on the Ethereum side (congestion, revert, insufficient gas, swap failure, etc. — situations explicitly acknowledged as possible for cross-chain bridges, as in the original LiFi report) triggers this exact behavior. The bug is deterministic given a `success: false` event, not merely theoretical.

### Recommendation
In `process_delivery_receipt`, branch on `receipt.success`:
- On `success == true`: keep current behavior (pay relayer, remove `PendingOrder`, emit `MessageDelivered`).
- On `success == false`: do not treat this as a completed/rewarded delivery. Instead, surface a distinct event (e.g. `MessageDispatchFailed`) and drive a refund path back to the original sender/beneficiary recorded for that nonce (or otherwise route the reserved/withdrawn assets on Asset Hub back to the depositor) before removing/resolving the `PendingOrder`, mirroring the "handle refunds when bridging" recommendation from the original report.

### Proof of Concept
1. On Asset Hub, execute an XCM with `WithdrawAsset` + `InitiateTransfer`/`ReserveWithdraw` targeting Ethereum, as in `send_weth_from_asset_hub_to_ethereum` — this withdraws WETH from the user and queues an outbound message with nonce `n` and a `PendingOrder { nonce, fee, .. }`.
2. Assume the corresponding Ethereum Gateway contract execution for nonce `n` reverts/fails (e.g., swap liquidity exhausted, analogous to the LiFi refund scenario), producing an `InboundMessageDispatched(nonce, topic, success=false, reward_address)` event on Ethereum.
3. A relayer submits `submit_delivery_receipt` with a valid proof of that event log.
4. `T::Verifier::verify` succeeds (the log/proof is genuine), `DeliveryReceipt::try_from` decodes `success: false` correctly, but `process_delivery_receipt` ignores it: it still pays the relayer reward and calls `<PendingOrders<T>>::remove(nonce)`, emitting `MessageDelivered`.
5. Result: the relayer is paid as if delivery succeeded, the pending-order bookkeeping for nonce `n` is destroyed, and the WETH withdrawn from the user in step 1 is never refunded — the funds are effectively lost from the user's perspective despite the on-chain proof showing the Ethereum dispatch failed.

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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L36-41)
```rust
//! 10. When the message has been verified and executed, the relayer will call the extrinsic
//!     `submit_delivery_receipt` to:
//! 	a. Verify the message with proof for a transaction receipt containing the event log,
//! 	   same as the inbound queue verification flow
//! 	b. Fetch the pending order by nonce of the message, pay reward with fee attached in the order
//!    	c. Remove the order from `PendingOrders` map storage by nonce
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

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound.rs (L65-82)
```rust
		let xcm = VersionedXcm::from(Xcm(vec![
			WithdrawAsset(assets.clone().into()),
			PayFees { asset: local_fee_asset.clone() },
			InitiateTransfer {
				destination: ethereum(),
				remote_fees: Some(AssetTransferFilter::ReserveWithdraw(Definite(
					remote_fee_asset.clone().into(),
				))),
				preserve_origin: true,
				assets: BoundedVec::truncate_from(vec![AssetTransferFilter::ReserveWithdraw(
					Definite(reserve_asset.clone().into()),
				)]),
				remote_xcm: Xcm(vec![DepositAsset {
					assets: Wild(AllCounted(2)),
					beneficiary: beneficiary(),
				}]),
			},
		]));
```
