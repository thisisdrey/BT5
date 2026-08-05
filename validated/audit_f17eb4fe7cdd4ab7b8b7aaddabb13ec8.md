Audit Report

## Title
`EthereumOutboundQueueV2::process_delivery_receipt` ignores the on-chain `success` flag, paying relayer rewards and permanently clearing settlement state for failed Ethereum-side deliveries - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

## Summary
`Pallet::process_delivery_receipt` decodes a `DeliveryReceipt` that carries a `success: bool` field sourced from Ethereum's `InboundMessageDispatched(nonce, topic, success, reward_address)` event, but never inspects `receipt.success` before paying the relayer reward and deleting the `PendingOrders` entry [1](#0-0) . A grep of the entire codebase confirms `success` is referenced only at the point of decoding in `DeliveryReceipt::try_from` and nowhere else is it branched on [2](#0-1) . Since assets bridged to Ethereum are burned on the Substrate side before the message is even queued, a truthful receipt reporting a failed Ethereum-side execution still results in reward payout and permanent removal of the settlement record, with no compensating refund path.

## Finding Description
The outbound flow burns assets immediately via `reanchor_and_burn_local` in the XCM executor's `do_reserve_withdraw_assets` [3](#0-2) , and the pallet records a `PendingOrder{nonce, fee, block_number}` for the corresponding outbound message [4](#0-3) .

When a relayer later submits `submit_delivery_receipt`, the pallet only verifies the cryptographic proof of the event log via `T::Verifier::verify` and decodes the `DeliveryReceipt` (including a `success: bool` field taken directly from the on-chain `InboundMessageDispatched` event) [5](#0-4) . `process_delivery_receipt` then unconditionally:
1. Pays `order.fee` to the relayer/reward account via `T::RewardPayment::register_reward`, and
2. Calls `<PendingOrders<T>>::remove(nonce)`, deleting the settlement record and permanently blocking resubmission (guarded by `InvalidPendingNonce`),

without ever checking `receipt.success` [6](#0-5) . There is no code elsewhere in the pallet, module, or wider codebase that inspects `receipt.success` to gate the reward or trigger a refund/mint-back of the already-burned assets — confirmed by the fact that `success` appears only in the decode implementation.

Any relayer — no collusion or privilege required — can submit a fully legitimate receipt for a genuine on-chain event where Ethereum-side Gateway execution reverted (`success: false`), and the pallet will treat it identically to a successful delivery.

## Impact Explanation
This violates the required invariant that settlement/payout state must only advance after end-to-end execution is confirmed successful. Concretely:
- Assets already burned on the Substrate side via `reanchor_and_burn_local` are never recovered when Ethereum-side execution fails, since `PendingOrders::remove(nonce)` erases the only on-chain record of the transfer and no refund/mint-back path exists — a permanent, unrecoverable user-fund loss.
- Relayers receive `order.fee` reward payouts for deliveries that did not actually succeed, which is an underpriced/incorrect settlement paid out of pallet-controlled reward funds regardless of true delivery outcome.

This matches the "permanent user-fund or bridge-state lock" and "duplicate settlement or payout" impact categories for the Polkadot SDK / Snowbridge scope.

## Likelihood Explanation
No malicious or privileged actor is needed. Ethereum-side reverts occur naturally (e.g., insufficient gas/value supplied for a `CallContract`/Transact command, or a downstream mint/call failure on the Gateway), and any relayer — even one acting completely honestly — can gather a valid proof for the resulting `InboundMessageDispatched(success: false)` event and submit it through the public `submit_delivery_receipt` extrinsic. `T::Verifier::verify` only checks proof-of-inclusion, not the semantic value of `success`, so this path is always reachable whenever a real Ethereum-side failure occurs.

## Recommendation
In `process_delivery_receipt`, branch on `receipt.success`:
- If `true`, proceed with the existing reward payout and `PendingOrders` removal.
- If `false`, withhold (or reduce) the relayer reward and avoid silently deleting settlement state; instead emit an event or transition to a state that allows refund/mint-back to the original sender, or otherwise preserves the failure for governance/user-driven recovery.

## Proof of Concept
1. A user's `InitiateTransfer`/`ReserveWithdraw` burns assets via `reanchor_and_burn_local` and the outbound queue records `PendingOrders[nonce] = {fee, ...}` [4](#0-3) .
2. On Ethereum, Gateway processing of the message reverts (e.g., insufficient gas in a `CallContract` command), and the contract emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. Any relayer collects the proof for this real event and calls `submit_delivery_receipt`; `T::Verifier::verify` succeeds and `DeliveryReceipt::try_from` decodes `success: false` correctly [7](#0-6) .
4. `process_delivery_receipt` never reads `receipt.success`, so it pays the relayer reward and calls `PendingOrders::remove(nonce)` regardless [6](#0-5) .
5. Result: the user's burned assets are unrecoverable (no refund path exists), the relayer is rewarded for a failed delivery, and the nonce cannot be resubmitted due to `InvalidPendingNonce` guarding `PendingOrders::get`.

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

**File:** polkadot/xcm/xcm-executor/src/lib.rs (L764-785)
```rust
	fn do_reserve_withdraw_assets(
		assets: AssetsInHolding,
		failed_bin: &mut AssetsInHolding,
		reserve: &Location,
		remote_xcm: &mut Vec<Instruction<()>>,
	) -> Result<Assets, XcmError> {
		// Must ensure that we recognise the assets as being managed by the destination.
		#[cfg(not(any(test, feature = "runtime-benchmarks")))]
		for asset in assets.assets_iter() {
			ensure!(
				Config::IsReserve::contains(&asset, &reserve),
				XcmError::UntrustedReserveLocation
			);
		}
		// Note that here we are able to place any assets which could not be
		// reanchored back into Holding (failed_bin).
		let reanchored_assets =
			assets.reanchor_and_burn_local(reserve, &Config::UniversalLocation::get(), failed_bin);
		remote_xcm.push(WithdrawAsset(reanchored_assets.clone()));

		Ok(reanchored_assets)
	}
```
