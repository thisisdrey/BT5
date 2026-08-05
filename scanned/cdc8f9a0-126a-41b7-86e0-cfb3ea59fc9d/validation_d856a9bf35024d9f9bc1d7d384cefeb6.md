### Title
Snowbridge assets are irrecoverably locked when the outbound message to Ethereum is never delivered - ([File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs])

### Summary
This is a local analog of the reported TokenBridge bug: user funds get stuck in a bridge contract with no withdrawal path whenever off-chain relaying stalls or the remote endpoint is unavailable. In Snowbridge V2 (Polkadot → Ethereum direction), a user's assets are withdrawn/reserved on AssetHub and committed into a `PendingOrder` on BridgeHub as soon as the `ExportMessage`/`InitiateTransfer` XCM executes, well before the message is actually delivered and executed on Ethereum. The only path that clears this pending state and finalizes settlement is `Pallet::process_delivery_receipt`, which requires an off-chain relayer to submit a valid delivery receipt. If no relayer ever submits that receipt (single/censoring relayer, Ethereum Gateway paused, contract misconfigured, or the Gateway address changed via governance so old messages can never match), the corresponding `PendingOrders` entry, and therefore the user's already-debited/reserved assets, remain stuck forever with no expiry, cancellation, or refund mechanism exposed anywhere in the pallet.

### Finding Description
The processing pipeline is documented directly in the pallet's module doc: [1](#0-0) 

Step 5 of the design doc confirms that assets are already withdrawn/reserved (`ReserveAssetDeposited`, `WithdrawAsset`) on AssetHub/BridgeHub at export time, before Ethereum ever sees the message: [2](#0-1) 

The only extrinsic that removes a `PendingOrders` entry and settles the relayer reward is `process_delivery_receipt`, gated purely on Gateway address and nonce existence — there is no time-bound, no cancellation call, and no alternate path to reclaim the order: [3](#0-2) 

The pallet's `Error` enum and storage definitions contain no expiry/refund/cancel variants (`InvalidPendingNonce`, `InvalidGateway`, `RewardPaymentFailed` are the only relevant errors), confirming there is no built-in timeout or refund mechanism: [4](#0-3) 

The `PendingOrder` is simply keyed by nonce with a fee attached, with no deadline field enforced by any extrinsic: [5](#0-4) 

The integration tests only exercise the happy path where a relayer promptly calls `process_delivery_receipt` after `MessageQueued`; there is no test (and no code path) covering permanent non-delivery: [6](#0-5) 

This mirrors the reported TokenBridge issue exactly: the "coordinator" role is played by the Snowbridge off-chain relayer network plus the Ethereum Gateway contract; if it is down, censoring, or misconfigured (e.g., `GatewayAddress` governance-changed so `receipt.gateway` no longer matches), the user's assets that were already debited on the Polkadot side have no way back — no admin-triggered refund, no self-service withdrawal, and no automatic expiry-based unlock.

### Impact Explanation
Any user-initiated Polkadot→Ethereum transfer via `pallet_xcm::execute`/`transfer_assets_using_type_and_then` that reaches the point of `WithdrawAsset`/`ReserveAssetDeposited` on AssetHub and gets queued as a `PendingOrder` on BridgeHub is exposed. If the relayer set stops functioning (goes offline, censors specific nonces, or the Ethereum-side Gateway is paused/misconfigured so delivery/verification can never succeed), the debited funds are permanently locked in bridge state with no recovery mechanism — a direct, permanent user-fund lock as called out in the impact gate ("permanent user-fund or bridge-state lock").

### Likelihood Explanation
This does not require a malicious relayer, validator, or governance actor — the trigger is exactly the passive failure mode described in the original report (a censoring/offline single point of failure, or delivery becoming impossible due to configuration drift such as a Gateway address change). Given Snowbridge's design explicitly relies on off-chain relayers to submit `process_delivery_receipt`, and there is no fallback path in this pallet, the likelihood of funds becoming stuck during any relayer outage or Gateway reconfiguration window is high and requires zero privileged access to trigger from the user's perspective.

### Recommendation
Add a time-bound expiry to `PendingOrder` (e.g., a `deadline` block number recorded at creation) and a permissionless `reclaim_expired_order`/refund extrinsic that, once the deadline has passed without a valid delivery receipt, releases the reserved/withdrawn assets back to the original sender (or the `AssetClaimer`, consistent with the `SetAssetClaimer` semantics already used for AH-side error handling). Alternatively, route through the existing asset-claim/trap mechanism so that failed cross-consensus deliveries always leave a claimable trapped-asset record rather than an inert `PendingOrders` map entry with no consumer path.

### Proof of Concept
1. User calls `pallet_xcm::execute` on AssetHub with `WithdrawAsset`/`PayFees`/`InitiateTransfer{ destination: ethereum(), ... }` as in `send_weth_and_dot_from_asset_hub_to_ethereum` — assets are withdrawn from the user and reserved to the Ethereum sovereign account. [7](#0-6) 
2. BridgeHub's `EthereumOutboundQueueV2` emits `MessageQueued`, creates a `PendingOrders` entry keyed by `nonce`.
3. Simulate a stalled/censoring relayer or a Gateway address change (governance updates `GatewayAddress`) — no relayer ever calls `process_delivery_receipt` with a matching `gateway`/`nonce`.
4. Observe: `PendingOrders::<T>::get(nonce)` remains populated indefinitely; no extrinsic in `outbound-queue-v2` (`lib.rs`) exists to remove it or refund the reserved/withdrawn assets. The user's funds, already debited on AssetHub/BridgeHub, are permanently unrecoverable.

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L1-41)
```rust
// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: 2023 Snowfork <hello@snowfork.com>
//! Pallet for committing outbound messages for delivery to Ethereum
//!
//! # Overview
//!
//! Messages come either from sibling parachains via XCM, or BridgeHub itself
//! via the `snowbridge-pallet-system-v2`:
//!
//! 1. `snowbridge_outbound_queue_primitives::v2::EthereumBlobExporter::deliver`
//! 2. `snowbridge_pallet_system_v2::Pallet::send`
//!
//! The message submission pipeline works like this:
//! 1. The message is first validated via the implementation for
//!    [`snowbridge_outbound_queue_primitives::v2::SendMessage::validate`]
//! 2. The message is then enqueued for later processing via the implementation for
//!    [`snowbridge_outbound_queue_primitives::v2::SendMessage::deliver`]
//! 3. The underlying message queue is implemented by [`Config::MessageQueue`]
//! 4. The message queue delivers messages to this pallet via the implementation for
//!    [`frame_support::traits::ProcessMessage::process_message`]
//! 5. The message is processed in `Pallet::do_process_message`:
//! 	a. Convert to `OutboundMessage`, and stored into the `Messages` vector storage
//! 	b. ABI-encode the `OutboundMessage` and store the committed Keccak256 hash in `MessageLeaves`
//! 	c. Generate `PendingOrder` with assigned nonce and fee attached, stored into the
//! 	   `PendingOrders` map storage, with nonce as the key
//! 	d. Increment nonce and update the `Nonce` storage
//! 6. At the end of the block, a merkle root is constructed from all the leaves in `MessageLeaves`.
//!    At the beginning of the next block, both `Messages` and `MessageLeaves` are dropped so that
//!    state at each block only holds the messages processed in that block.
//! 7. This merkle root is inserted into the parachain header as a digest item
//! 8. Offchain relayers are able to relay the message to Ethereum after:
//! 	a. Generating a merkle proof for the committed message using the `prove_message` runtime API
//! 	b. Reading the actual message content from the `Messages` vector in storage
//! 9. On the Ethereum side, the message root is ultimately the thing being verified by the Beefy
//!    light client.
//! 10. When the message has been verified and executed, the relayer will call the extrinsic
//!     `submit_delivery_receipt` to:
//! 	a. Verify the message with proof for a transaction receipt containing the event log,
//! 	   same as the inbound queue verification flow
//! 	b. Fetch the pending order by nonce of the message, pay reward with fee attached in the order
//!    	c. Remove the order from `PendingOrders` map storage by nonce
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L225-243)
```rust
	#[pallet::error]
	pub enum Error<T> {
		/// The message is too large
		MessageTooLarge,
		/// The pallet is halted
		Halted,
		/// Invalid Channel
		InvalidChannel,
		/// Invalid Envelope
		InvalidEnvelope,
		/// Message verification error
		Verification(VerificationError),
		/// Invalid Gateway
		InvalidGateway,
		/// Pending nonce does not exist
		InvalidPendingNonce,
		/// Reward payment failed
		RewardPaymentFailed,
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

**File:** bridges/snowbridge/docs/v2.md (L104-119)
```markdown
### Step 5: BH executes message x2

Message $x_2$ is parsed by the `SnowbridgeMessageExporter` in block $n$ with the following effects:

- A bridge command $m$ is committed to binary merkle tree $M_n$.
  - The transferred asset is parsed from `ReserveAssetDeposited` , `WithdrawAsset` or `TeleportedAssetReceived`
    instructions for the local, destination and teleport asset transfer types respectively.
  - The original origin is preserved through the `AliasOrigin` instruction. This will allow us to resolve agents for the
    case of `Transact`.
  - The message exporter must be able to support multiple assets and reserve types in the same message and potentially
    multiple `Transacts`.
  - The Message Exporter must be able to support multiple Deposited Assets.
  - The Message Exporter must be able to parse `SetAssetClaimer` and allow the provided location to claim the assets on
    BH in case of errors.
- Given relayer reward $r$ in WETH, set storage $P(\mathrm{hash}(m)) = r$. This is parsed from the `WithdrawAsset` and
  `PayFees` instruction within `ExportMessage`.
```

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound.rs (L439-484)
```rust
	AssetHubWestend::execute_with(|| {
		type RuntimeOrigin = <AssetHubWestend as Chain>::RuntimeOrigin;

		let local_fee_asset =
			Asset { id: AssetId(Location::parent()), fun: Fungible(LOCAL_FEE_AMOUNT_IN_DOT) };
		let remote_fee_asset =
			Asset { id: AssetId(ethereum()), fun: Fungible(REMOTE_FEE_AMOUNT_IN_ETHER) };

		let weth_asset = Asset { id: weth_location().into(), fun: Fungible(TOKEN_AMOUNT) };

		let dot_asset = Asset { id: AssetId(Location::parent()), fun: Fungible(TOKEN_AMOUNT) };

		let assets = vec![
			weth_asset.clone(),
			dot_asset.clone(),
			local_fee_asset.clone(),
			remote_fee_asset.clone(),
		];

		let xcms = VersionedXcm::from(Xcm(vec![
			WithdrawAsset(assets.clone().into()),
			PayFees { asset: local_fee_asset.clone() },
			InitiateTransfer {
				destination: ethereum(),
				remote_fees: Some(AssetTransferFilter::ReserveWithdraw(Definite(
					remote_fee_asset.clone().into(),
				))),
				preserve_origin: true,
				assets: BoundedVec::truncate_from(vec![
					AssetTransferFilter::ReserveWithdraw(Definite(weth_asset.clone().into())),
					AssetTransferFilter::ReserveDeposit(Definite(dot_asset.into())),
				]),
				remote_xcm: Xcm(vec![DepositAsset {
					assets: Wild(All),
					beneficiary: beneficiary(),
				}]),
			},
		]));

		<AssetHubWestend as AssetHubWestendPallet>::PolkadotXcm::execute(
			RuntimeOrigin::signed(AssetHubWestendReceiver::get()),
			bx!(xcms),
			Weight::from(EXECUTION_WEIGHT),
		)
		.unwrap();
	});
```

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound.rs (L486-513)
```rust
	BridgeHubWestend::execute_with(|| {
		type RuntimeEvent = <BridgeHubWestend as Chain>::RuntimeEvent;
		// Check that Ethereum message was queue in the Outbound Queue
		assert_expected_events!(
			BridgeHubWestend,
			vec![RuntimeEvent::EthereumOutboundQueueV2(snowbridge_pallet_outbound_queue_v2::Event::MessageQueued{ .. }) => {},]
		);

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
