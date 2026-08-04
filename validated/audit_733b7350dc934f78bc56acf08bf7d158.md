Confirmed: `DeliveryReceipt.success` is decoded from the Ethereum `InboundMessageDispatched` event but is never inspected in `process_delivery_receipt`, which unconditionally pays the relayer reward and removes the pending order regardless of on-chain execution outcome. [1](#0-0) [2](#0-1) [3](#0-2) 

### Title
`snowbridge-pallet-outbound-queue-v2::process_delivery_receipt` ignores `DeliveryReceipt.success`, rewarding relayers and closing pending orders for reverted Ethereum command execution - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
The Snowbridge Outbound Queue V2 pallet decodes an on-chain Ethereum event, `InboundMessageDispatched(nonce, topic, success, reward_address)`, into a `DeliveryReceipt` struct that carries an explicit `success: bool` field indicating whether the Gateway contract actually executed the outbound commands successfully on Ethereum. The extrinsic `submit_delivery_receipt` verifies the event proof and passes the decoded receipt to `process_delivery_receipt`, but that function never reads `receipt.success`. It unconditionally pays the relayer reward from `PendingOrders` and removes the order, emitting `Event::MessageDelivered`, whether the receipt reports success or failure.

### Finding Description
`DeliveryReceipt` is defined with a dedicated `success` field decoded straight from the Ethereum event log: [1](#0-0) 

`submit_delivery_receipt` verifies the proof and constructs the receipt, then forwards it to `process_delivery_receipt`: [3](#0-2) 

`process_delivery_receipt` only checks `receipt.gateway` and the `nonce`-keyed `PendingOrders` entry. It never inspects `receipt.success`: [2](#0-1) 

This is the direct structural analog of the reported CRE bug: a reply/receipt structure carries a status field describing whether the remote-side execution actually succeeded (`txStatus`/`receiverContractExecutionStatus` in the external report, `success` here), and the caller treats every reply as success, consuming the "resource" (gas/fee) and advancing state (closing the round / removing the pending order and paying the reward) without checking it.

### Impact Explanation
Because `success` is unused, any Ethereum-side command execution failure (e.g., the Gateway's dispatch of a `Transact`/asset-unlock command reverts due to insufficient gas, contract-side checks, or an intermediate agent failure) still results in:
- the relayer being paid the full fee from `PendingOrders` as if delivery succeeded,
- the `PendingOrders` entry being permanently removed, and
- `Event::MessageDelivered` being emitted, giving operators/dashboards a false signal of success.

Once the `PendingOrder` is removed, there is no other mechanism in this pallet to retry, flag, or compensate for the failed remote execution — the bridge's on-chain bookkeeping for that message is finalized as "delivered" even though the corresponding value transfer or command never took effect on Ethereum. This causes duplicate/incorrect settlement of relayer rewards (fee paid for a failed job) and silently drops the true delivery status of bridged messages, which can mask systematic command failures (e.g. mis-configured gas commitments) exactly as in the source report, but here it also directly mispays value (`RewardPayment::register_reward`) for work that was never actually completed on the destination chain.

### Likelihood Explanation
The `success` flag is attacker/relayer-controlled only insofar as the relayer chooses which real Ethereum event/proof to submit — this is not a spoofable value (it is decoded from a verified log), but the flag being emitted as `false` is a normal, expected outcome of Ethereum-side execution failures (e.g., out-of-gas dispatch of a command in the Gateway), which is a realistic occurrence, not a contrived edge case. Every relayer submitting a legitimate delivery-receipt proof for a message whose remote execution failed will trigger this path unconditionally, making the likelihood high whenever any outbound command fails to execute on the Ethereum side.

### Recommendation
In `process_delivery_receipt`, branch on `receipt.success`:
- If `true`, proceed as today (pay reward, remove `PendingOrders`, emit `MessageDelivered`).
- If `false`, do not pay the relayer reward for the failed command execution (or pay only a reduced "proof of delivery" reward if that is the intended design), emit a distinct event (e.g. `MessageDeliveryFailed { nonce }`), and decide explicitly whether the `PendingOrders` entry should be removed, retried, or retained for manual/governance handling, so that failed executions are auditable and don't silently look identical to successful ones.

### Proof of Concept
1. A message is queued via `do_process_message`, creating a `PendingOrder` for `nonce = N` with `fee = F`.
2. On Ethereum, the Gateway attempts to execute the outbound command for nonce `N` but the command dispatch reverts (e.g., insufficient gas budget for the destination call), so the Gateway emits `InboundMessageDispatched(nonce=N, topic, success=false, reward_address)`.
3. A relayer captures this event, builds the proof, and calls `submit_delivery_receipt(origin, event)`. [3](#0-2) 
4. `T::Verifier::verify` succeeds (the proof is genuine), `DeliveryReceipt::try_from` decodes `success: false` correctly.
5. `process_delivery_receipt` is called; it checks `receipt.gateway` (matches) and fetches `PendingOrders::get(N)` (present with `fee = F`), then unconditionally pays `T::RewardPayment::register_reward(&reward_account, ..., F)` and removes `PendingOrders::<T>::remove(N)`, emitting `Event::MessageDelivered { nonce: N }` — exactly as if the command had executed successfully on Ethereum. [4](#0-3) 
6. The existing integration test fixture already demonstrates the call succeeding with `success: true`; substituting `success: false` in the same fixture reproduces identical control flow and identical `RewardRegistered`/`MessageDelivered` events, proving the field is dead for control-flow purposes. [5](#0-4)

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
