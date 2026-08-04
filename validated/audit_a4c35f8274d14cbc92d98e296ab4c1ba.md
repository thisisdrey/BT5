### Title
`process_delivery_receipt` in `snowbridge-pallet-outbound-queue-v2` ignores `DeliveryReceipt.success`, always paying relayer reward and settling the order as delivered even when the Ethereum-side dispatch failed - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
This is a direct structural analog of the reported `settlement.cairo` bug: a delivery/callback status field that is supposed to distinguish a successful destination-side execution from a failed one is decoded off-chain data but never consulted when updating the source-side settlement state. In `snowbridge-pallet-outbound-queue-v2`, the `DeliveryReceipt` decoded from the Ethereum `InboundMessageDispatched` event log carries a `success: bool` field [1](#0-0) , but `Pallet::process_delivery_receipt` never reads `receipt.success` — it unconditionally pays the relayer reward, removes the `PendingOrder`, and emits `MessageDelivered` as long as the extrinsic itself doesn't error [2](#0-1) .

### Finding Description
The outbound message lifecycle is: a message is queued via `do_process_message`, which creates a `PendingOrder{nonce, fee, block_number}` recording the fee owed to whichever relayer eventually proves delivery [3](#0-2) . When the message is executed on Ethereum, the Gateway contract emits `InboundMessageDispatched(nonce, topic, success, reward_address)`, and this log is decoded into a `DeliveryReceipt` struct with an explicit `success: bool` field representing whether dispatch on Ethereum succeeded [4](#0-3) .

`process_delivery_receipt` is the function that resolves the pending order using this receipt:

```rust
pub fn process_delivery_receipt(
    relayer: <T as frame_system::Config>::AccountId,
    receipt: DeliveryReceipt,
) -> DispatchResult
where ... {
    ensure!(T::GatewayAddress::get() == receipt.gateway, Error::<T>::InvalidGateway);
    let reward_account = ...;
    let nonce = receipt.nonce;
    let order = <PendingOrders<T>>::get(nonce).ok_or(Error::<T>::InvalidPendingNonce)?;
    if order.fee > 0 {
        T::RewardPayment::register_reward(&reward_account, T::DefaultRewardKind::get(), order.fee);
    }
    <PendingOrders<T>>::remove(nonce);
    Self::deposit_event(Event::MessageDelivered { nonce });
    Ok(())
}
``` [2](#0-1) 

`receipt.success` is checked against `T::GatewayAddress`, `receipt.reward_address`, and `receipt.nonce`, but the `success` field itself is never referenced anywhere in this pallet (confirmed via search — no match for `receipt.success` or `.success` in the pallet source). This means:
- Whether the destination-side (Ethereum) command execution actually succeeded or failed, the source-chain (BridgeHub) accounting always treats it identically: pay the relayer, remove the pending order, and emit `MessageDelivered` — a terminal "delivered/settled" signal.
- There is no `MessageDeliveryFailed` event, no distinct handling path, and no state that preserves the failure for downstream consumers (e.g., XCM sender / asset-transfer logic) to react to (such as reversing a reserve-withdraw or notifying the original sender that funds are stuck on Ethereum).

This mirrors exactly the Cairo bug: `cross_chain_msg_status` was decoded and passed into the function but ignored in favor of unconditional `SUCCESS` as long as the call path executed without reverting. Here, `receipt.success` is decoded but ignored in favor of unconditional reward-payout/order-removal as long as the extrinsic doesn't error.

### Impact Explanation
Outbound messages to Ethereum, in the v2 flow, carry cross-chain asset transfers (`InitiateTransfer`/reserve-withdraw of DOT/WETH per the integration tests) [5](#0-4) . If a command fails to execute on the Ethereum Gateway (e.g., an `AgentExecute`/token-mint command reverts on the Ethereum side), the relayer can still submit the `DeliveryReceipt` with `success: false`, and the pallet will:
1. Pay out the relayer's fee reward regardless of failure (public underpriced/incorrect-payout condition — the relayer gets rewarded for a failed delivery).
2. Permanently remove the `PendingOrder`, destroying any bookkeeping that a retry or reconciliation mechanism could use.
3. Emit `MessageDelivered`, which downstream systems (e.g., off-chain agents, governance, or other pallets) may treat as confirmation that assets were safely transferred to Ethereum, when in fact the transfer failed and the assets are effectively lost/stuck (already burned/withdrawn on the source side with no compensating action on the destination side).

This is a duplicate/incorrect settlement issue: source-side state is marked "delivered" independent of actual destination outcome, causing possible fund loss for the original message sender (assets withdrawn from AssetHub/BridgeHub reserve but never delivered on Ethereum) while the relayer is still rewarded — directly matching the "theft or unbacked mint/unlock" and "duplicate settlement or payout" impact categories in scope.

### Likelihood Explanation
Likelihood is high: `process_delivery_receipt` is called from the public extrinsic `submit_delivery_receipt`, callable by any signed account (a relayer) supplying a valid Merkle/beacon proof for the event log [6](#0-5) . The relayer does not need to control or falsify the `success` flag — it is emitted honestly by the Ethereum Gateway when a dispatched command genuinely fails, and any relayer (not just a malicious one) faithfully relaying a legitimate failed-dispatch event log triggers the same unconditional reward/removal path. No special privilege, governance action, or malicious infrastructure is required — this is a normal-operation bug that manifests whenever a legitimate command execution fails on Ethereum for any reason (e.g., insufficient gas, contract-side revert, agent misconfiguration).

### Recommendation
Branch on `receipt.success` in `process_delivery_receipt`:
- On `success == true`: proceed as today (pay reward, remove order, emit `MessageDelivered`).
- On `success == false`: do not treat the order as successfully settled. Emit a distinct `MessageDeliveryFailed { nonce }` event, and either retain/mark the order state for reconciliation, or apply a separate settlement path that reflects the failure (e.g., withhold/adjust relayer reward per protocol, and surface the failure so that reserve funds already withdrawn on the source chain can be reconciled or refunded rather than silently lost).

### Proof of Concept
1. Queue an outbound message via `do_process_message`, creating `PendingOrder{nonce, fee, ...}` (as in existing tests, e.g. `send_weth_from_asset_hub_to_ethereum`).
2. Construct a `DeliveryReceipt` with `success: false` (using a mock/decoded `InboundMessageDispatched` event log where `success` is `false`), valid `gateway`, matching `nonce`, and any `reward_address`.
3. Call `EthereumOutboundQueueV2::submit_delivery_receipt` (or directly `process_delivery_receipt`) with this receipt.
4. Observe: `RewardRegistered`/reward payout still occurs, `PendingOrders::<T>::get(nonce)` becomes `None`, and `Event::MessageDelivered { nonce }` is emitted — identical to the `success: true` case demonstrated in the existing test `submit_delivery_receipt_succeeds_after_unhalt` [7](#0-6)  — despite the underlying Ethereum dispatch having failed, confirming the source-chain settlement state does not reflect actual destination-chain outcome.

### Citations

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/delivery_receipt.rs (L10-51)
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

#[derive(Copy, Clone, Encode, Decode, Eq, PartialEq, Debug, TypeInfo)]
pub enum DeliveryReceiptDecodeError {
	DecodeLogFailed,
	DecodeAccountFailed,
}

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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L426-440)
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

			<Nonce<T>>::set(nonce);

			Self::deposit_event(Event::MessageAccepted { id, nonce });
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

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound.rs (L65-90)
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

		// Send the Weth back to Ethereum
		<AssetHubWestend as AssetHubWestendPallet>::PolkadotXcm::execute(
			RuntimeOrigin::signed(AssetHubWestendReceiver::get()),
			bx!(xcm),
			Weight::from(EXECUTION_WEIGHT),
		)
		.unwrap();
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/test.rs (L418-449)
```rust
// After governance resumes the bridge, legitimate delivery receipts flow through again:
// the order is paid out and removed from storage.
#[test]
fn submit_delivery_receipt_succeeds_after_unhalt() {
	new_tester().execute_with(|| {
		let nonce = 0;
		let fee: u128 = 1_000_000;
		let order = PendingOrder { nonce, fee, block_number: System::block_number() };
		PendingOrders::<Test>::insert(nonce, order);

		let relayer: AccountId32 = [7u8; 32].into();
		let origin = RuntimeOrigin::signed(relayer);
		let event = Box::new(mock_valid_event_proof());

		// Bridge halted — receipt rejected, order untouched.
		set_verifier_halted(true);
		assert_noop!(
			OutboundQueue::submit_delivery_receipt(origin.clone(), event.clone()),
			Error::<Test>::Verification(VerificationError::Halted)
		);
		assert!(PendingOrders::<Test>::get(nonce).is_some());

		// Bridge resumed — same receipt succeeds and the order is settled.
		set_verifier_halted(false);
		assert_ok!(OutboundQueue::submit_delivery_receipt(origin, event));
		assert!(PendingOrders::<Test>::get(nonce).is_none());

		System::assert_has_event(mock::RuntimeEvent::OutboundQueue(Event::MessageDelivered {
			nonce,
		}));
	});
}
```
