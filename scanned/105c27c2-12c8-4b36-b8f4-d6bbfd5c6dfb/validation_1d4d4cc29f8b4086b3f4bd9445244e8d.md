### Title
`submit_delivery_receipt` pays relayer reward and clears `PendingOrder` without checking `DeliveryReceipt.success`, settling failed Ethereum executions as if they succeeded - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
The core broken invariant in the external report is: *cross-chain settlement state is finalized (fee paid / status marked terminal) without validating whether the destination-side execution actually succeeded, so no refund/replay path exists for failed messages.* The `snowbridge-pallet-outbound-queue-v2` reproduces this exact pattern in the Snowbridge outbound (BridgeHub → Ethereum) delivery-receipt settlement flow: the `success` flag decoded from the Ethereum `InboundMessageDispatched` event is never inspected before the pallet pays the relayer reward and permanently removes the `PendingOrder`.

### Finding Description
`DeliveryReceipt` is decoded straight from the Ethereum Gateway's `InboundMessageDispatched(nonce, topic, success, reward_address)` event log: [1](#0-0) 

`submit_delivery_receipt` verifies the proof, decodes the receipt, and calls `process_delivery_receipt`: [2](#0-1) 

`process_delivery_receipt` then unconditionally pays the reward tied to the `PendingOrder` and removes the order, without ever reading `receipt.success`: [3](#0-2) 

The `success` field is present on the decoded `DeliveryReceipt` struct and is asserted only in test fixtures (e.g. `success: true` in integration tests), but is never checked in the production code path. As a result, whenever the Gateway on Ethereum emits `InboundMessageDispatched` with `success = false` (i.e., the commands — unlock/mint/transact — reverted or failed to execute on Ethereum), the pallet still:
1. Pays the relayer their full fee/reward from `order.fee`, and
2. Removes the `PendingOrder`, permanently marking the message as `MessageDelivered`,

even though the intended destination-side effect (asset unlock/mint to the beneficiary, or `Transact`) never happened. There is no distinct failure path, no event indicating failure, and no mechanism to refund the assets that were locked/burned on BridgeHub/AssetHub when the message was originally queued (analogous to `ChakraSettlementHandler.cross_chain_erc20_settlement`'s lock/burn with no corresponding unlock/mint on failure). This directly matches the required pivot: *"Message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically."* Here settlement advances (`PendingOrders::remove` + reward payment) regardless of the actual execution outcome encoded in the very receipt meant to convey that outcome.

### Impact Explanation
- Relayers are rewarded for delivering messages whose on-chain effects (transferring locked/reserved user assets, or performing `Transact`) failed on Ethereum, i.e. underpriced/incorrect public settlement work is paid out from real user/protocol funds.
- Users whose assets were withdrawn/reserved on AssetHub/BridgeHub for the cross-chain transfer have no recourse: the `PendingOrder` is deleted and the message is marked `MessageDelivered`, closing off any way to detect or later remediate the failure — a permanent fund loss/lock consistent with the "H-11" bug class (loss of tokens on failed cross-chain settlement with no refund path).
- This is fully reachable by any unprivileged, honest relayer submitting a legitimate delivery receipt for a message that genuinely failed on Ethereum — no malicious relayer, prover, or governance actor is required; the flaw is in the pallet's logic, not in any adversarial input.

### Likelihood Explanation
Any Ethereum-side execution failure (e.g., insufficient gas provided by `GasMeter`, a reverting `Transact` call, or a token contract rejecting mint/unlock) naturally produces `success = false` in the Gateway's emitted event. A relayer following the documented flow (submit the proof for whatever event is emitted) triggers this path without any special conditions, making this a high-likelihood, easily triggered scenario in normal operation, not an edge case requiring privileged access.

### Recommendation
In `process_delivery_receipt`, branch on `receipt.success`:
- On `success == true`: keep current behavior (pay reward, remove order).
- On `success == false`: do not pay the relayer the full delivery reward for a failed execution (or pay only a reduced "delivery" reward while withholding execution-dependent value), emit a distinct `MessageDeliveryFailed`/`MessageExecutionFailed` event carrying the `nonce`/`topic`, and expose a way to trigger refund/unlock of the originally reserved/burned assets back to the original sender (or route them to a claimable location), mirroring how `AssetsTrapped`/`claim_assets` already lets users recover assets in other Snowbridge V2 failure paths.

### Proof of Concept
1. User sends WETH/DOT from AssetHub to Ethereum via `InitiateTransfer`, causing `ReserveAssetDeposited`/`WithdrawAsset` to lock/burn the user's assets on AssetHub, and the message is queued with a `PendingOrder{nonce, fee}` in `snowbridge-pallet-outbound-queue-v2`.
2. On Ethereum, the Gateway's `InboundMessageDispatched` execution fails (e.g., `Transact`/mint reverts due to insufficient gas or a receiving contract rejecting the call), emitting `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. A relayer submits this event with a valid proof via `submit_delivery_receipt`.
4. `T::Verifier::verify` succeeds (the proof is valid — only the *contained event* records failure), `DeliveryReceipt::try_from` decodes `success = false`, and `process_delivery_receipt` runs:
```rust
// bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs
let order = <PendingOrders<T>>::get(nonce).ok_or(Error::<T>::InvalidPendingNonce)?;
if order.fee > 0 {
    T::RewardPayment::register_reward(&reward_account, T::DefaultRewardKind::get(), order.fee);
}
<PendingOrders<T>>::remove(nonce);
Self::deposit_event(Event::MessageDelivered { nonce });
```
`receipt.success` is never read, so the relayer is paid and the order is deleted exactly as if the message had succeeded, even though the intended asset unlock/mint on Ethereum never took place — leaving the user's originally locked/burned funds permanently unaccounted for with no refund mechanism.

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
