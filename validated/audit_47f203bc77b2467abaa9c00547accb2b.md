Audit Report

## Title
`process_delivery_receipt` in Snowbridge outbound-queue-v2 ignores the on-chain `success` flag, rewarding relayers and clearing pending orders even when the Ethereum-side execution failed - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

## Summary
The `DeliveryReceipt` type decoded from the Ethereum `InboundMessageDispatched` event carries a `success: bool` field <cite repo="ThankGodontt/polkadot-sdk--004" path="bridges/snowbridge/primitives/outbound-queue/src/v2/delivery_receipt.rs" start="14="27" /> reflecting whether the message actually dispatched successfully on the Gateway contract, but `Pallet::process_delivery_receipt` never inspects this field before paying the relayer reward and removing the `PendingOrder` [1](#0-0) . Since assets are already withdrawn from the user on Asset Hub via `WithdrawAsset`/`InitiateTransfer`/`ReserveWithdraw` before the message ever reaches Ethereum [2](#0-1) , a failed Ethereum-side dispatch (`success: false`) is settled identically to a successful one, permanently discarding the bookkeeping needed to trigger any refund.

## Finding Description
`submit_delivery_receipt` verifies the event log/proof, decodes the `DeliveryReceipt`, and forwards it straight to `process_delivery_receipt` [3](#0-2) . Within `process_delivery_receipt`, the only checks performed are on `receipt.gateway` (matching `GatewayAddress`) and the existence of a `PendingOrder` for `receipt.nonce`; `order.fee` is then paid to the resolved reward account, the order is unconditionally removed via `<PendingOrders<T>>::remove(nonce)`, and `MessageDelivered` is emitted — `receipt.success` is never read [4](#0-3) . This is confirmed by the pallet's own doc comment, which states the receipt flow simply "verifies proof," "fetch[es] the pending order," "pay[s] reward," and "remove[s] the order," with no mention of a failure/success branch [5](#0-4) . The integration and unit test suites for this pallet exclusively construct receipts with `success: true` and never exercise a `success: false` code path [6](#0-5) , and the `AddTip` implementation in this pallet also has no notion of `success` [7](#0-6) . No other code path in this pallet re-examines `success` to unlock or reroute assets.

## Impact Explanation
This maps to the "duplicate settlement or payout" / "permanent user-fund lock" impact category in the Polkadot SDK gate: assets withdrawn on Asset Hub for an Ethereum-bound transfer are consumed irrevocably regardless of whether the corresponding Ethereum dispatch succeeded, since the settlement logic treats `success: false` exactly like `success: true` — paying the relayer and deleting the `PendingOrder` (the only state tracking that nonce) either way.

## Likelihood Explanation
This is reachable via the ordinary, unprivileged `submit_delivery_receipt` extrinsic once any relayer obtains a valid Ethereum event-log proof for a nonce that legitimately failed on the Gateway contract (e.g., revert, insufficient gas, downstream dispatch failure) — no malicious relayer, validator, or governance action is required, since a `success: false` event is just as provable as `success: true`.

## Recommendation
In `process_delivery_receipt`, branch on `receipt.success`: on `true`, keep current behavior; on `false`, avoid rewarding the relayer as a successful delivery and instead emit a distinct failure event and drive/enable a refund path for the assets reserved on Asset Hub for that nonce before resolving the `PendingOrder`.

## Proof of Concept
1. On Asset Hub, execute an XCM with `WithdrawAsset` + `InitiateTransfer`/`ReserveWithdraw` targeting Ethereum (as in `send_weth_from_asset_hub_to_ethereum` [8](#0-7) ), withdrawing WETH from the user and creating a `PendingOrder` for nonce `n`.
2. Assume the Ethereum Gateway contract execution for nonce `n` reverts, producing `InboundMessageDispatched(n, topic, success=false, reward_address)`.
3. A relayer submits `submit_delivery_receipt` with a valid proof of that log; `DeliveryReceipt::try_from` decodes `success: false` correctly.
4. `process_delivery_receipt` still pays the relayer reward from `order.fee` and calls `<PendingOrders<T>>::remove(n)`, emitting `MessageDelivered` — identical to the success path shown in existing tests [9](#0-8) .
5. Result: the relayer is rewarded as if delivery succeeded, the pending-order bookkeeping for nonce `n` is destroyed, and the WETH withdrawn from the user is never refunded.

### Citations

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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L483-496)
```rust
	impl<T: Config> AddTip for Pallet<T> {
		fn add_tip(nonce: u64, amount: u128) -> Result<(), AddTipError> {
			ensure!(amount > 0, AddTipError::AmountZero);
			PendingOrders::<T>::try_mutate_exists(nonce, |maybe_order| -> Result<(), AddTipError> {
				match maybe_order {
					Some(order) => {
						order.fee = order.fee.saturating_add(amount);
						Ok(())
					},
					None => Err(AddTipError::UnknownMessage),
				}
			})
		}
	}
```

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound.rs (L44-91)
```rust
#[test]
fn send_weth_from_asset_hub_to_ethereum() {
	fund_on_bh();

	register_assets_on_ah();

	fund_on_ah();

	AssetHubWestend::execute_with(|| {
		type RuntimeOrigin = <AssetHubWestend as Chain>::RuntimeOrigin;

		let local_fee_asset =
			Asset { id: AssetId(Location::parent()), fun: Fungible(LOCAL_FEE_AMOUNT_IN_DOT) };

		let remote_fee_asset =
			Asset { id: AssetId(ethereum()), fun: Fungible(REMOTE_FEE_AMOUNT_IN_ETHER) };

		let reserve_asset = Asset { id: AssetId(weth_location()), fun: Fungible(TOKEN_AMOUNT) };

		let assets = vec![reserve_asset.clone(), remote_fee_asset.clone(), local_fee_asset.clone()];

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
	});
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
