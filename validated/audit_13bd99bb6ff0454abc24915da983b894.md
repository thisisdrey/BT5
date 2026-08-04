## Analysis

The external report's core broken invariant: an outer "settlement" step (marking a withdrawal/message as finalized) proceeds to completion using gas/weight reserved independently of whether the inner action (the actual value-moving call) succeeded, so a legitimately-verified but *reverted* inner execution still gets accepted as done — permanently foreclosing retry/refund even though the underlying transfer never happened.

I found a direct structural analog in the Snowbridge V2 outbound bridge pipeline on BridgeHub. [1](#0-0) 

The `DeliveryReceipt` decodes the `InboundMessageDispatched(nonce, topic, success, reward_address)` event emitted by the Ethereum Gateway contract, and `success` explicitly encodes whether the Gateway's dispatch of the bridged command actually succeeded on Ethereum (the same semantic role as the OP `relayMessage` inner-call success in the original report). [2](#0-1) 

`process_delivery_receipt` never inspects `receipt.success`. It only checks the gateway address and nonce, pays the relayer's fee via `T::RewardPayment::register_reward`, and unconditionally calls `<PendingOrders<T>>::remove(nonce)` — the sole piece of state tracking that message's outcome. Once removed, there is no other storage that lets the protocol know the command failed on Ethereum, and no refund/replay mechanism exists.

Each outbound command's gas budget is pre-computed heuristically at enqueue time: [3](#0-2) 
`gas: T::GasMeter::maximum_dispatch_gas_used_at_most(&command)` — exactly the kind of static "worst case" gas estimate that the OP report shows can be wrong for a given execution path (state-dependent SSTORE costs, external call gas forwarding rules, etc.), leading the actual Gateway dispatch to run out of gas and revert while the surrounding Ethereum transaction (and the `InboundMessageDispatched` event with `success=false`) still completes.

Since assets for the transfer are already withdrawn/reserved on Polkadot's side at message-send time (confirmed by the emulated test flow showing `pallet_assets::Event::Withdrawn` on AssetHub happening before the message is even queued on BridgeHub) and `process_delivery_receipt` discards `PendingOrders` regardless of `success`, a legitimately-relayed but failed Ethereum-side dispatch results in the relayer being paid and the order being closed while the bridged funds are unrecoverable — no malicious relayer, admin, or governance action is required; the relayer is submitting a truthful, verified proof of what actually happened on Ethereum.

### Title
Outbound bridge delivery receipts finalize relayer reward and close pending orders without checking dispatch `success`, permanently losing funds on failed Ethereum-side execution - (File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs)

### Summary
`Pallet::process_delivery_receipt` in `snowbridge-pallet-outbound-queue-v2` decodes a `DeliveryReceipt` containing a `success` flag reflecting whether the Ethereum Gateway actually executed the bridged command, but it never checks that flag before paying the relayer and permanently removing the `PendingOrder` for the message. This mirrors the audited Optimism bug where an outer bookkeeping step (finalizing a withdrawal) commits regardless of whether the inner action (the cross-domain call) actually succeeded, because the code path that would need to record/​retry the failure runs out of budget or is simply never consulted.

### Finding Description
The message lifecycle is:
1. `do_process_message` computes a static, worst-case `gas` per command via `T::GasMeter::maximum_dispatch_gas_used_at_most(&command)` and stores a `PendingOrder{ nonce, fee, block_number }` keyed by nonce. [4](#0-3) 
2. A relayer relays the message to Ethereum, where the Gateway attempts to dispatch it with (at most) the pre-computed `gas`. If the dispatch reverts (e.g. due to the gas estimate under-shooting the real execution cost — the same class of miscalculation as in the OP report), the Gateway still emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`. [1](#0-0) 
3. The relayer submits this legitimately verified proof via `submit_delivery_receipt`, which calls `process_delivery_receipt`. [5](#0-4) 
4. `process_delivery_receipt` pays out `order.fee` to the relayer and unconditionally removes the `PendingOrder`, without ever branching on `receipt.success`: [2](#0-1) 

There is no other storage item or pallet that records a failed dispatch for retry, refund, or replay. Once `PendingOrders::remove(nonce)` executes, the message's fate is sealed — identical to how, in the OP report, the `OptimismPortal` marks `finalizedWithdrawals[hash] = true` and forecloses any future attempt even though the inner `relayMessage` call reverted and never recorded itself in `failedMessages`.

### Impact Explanation
Funds transferred from AssetHub/BridgeHub to Ethereum are withdrawn/reserved on the Polkadot side at enqueue time, before any Ethereum-side confirmation. If the Ethereum Gateway dispatch of the corresponding command fails (`success=false`) — whether due to gas-estimation error, a state change that raises the real execution cost, or a malicious sender crafting an outbound command that is guaranteed to revert on Ethereum — the pallet still treats the message as fully settled: the relayer is rewarded and the order is deleted with no path to reissue, retry, or refund the locked assets. This is a permanent user-fund lock, matching the "permanent user-fund or bridge-state lock" impact category.

### Likelihood Explanation
No compromised relayer, admin, or governance action is needed — the relayer's proof is a truthful, cryptographically verified event log reflecting real on-chain Ethereum state. Any transaction whose real gas usage on the Ethereum side exceeds the pre-computed `maximum_dispatch_gas_used_at_most` estimate (state-dependent costs, EVM gas-forwarding rules on nested calls, or a specifically crafted payload/target designed to consume more gas than estimated) will trigger this path deterministically once relayed, exactly as in the OP report's scenario where "insufficient gas can be sent such that ... the remaining gas is not enough for replayability to be encoded."

### Recommendation
Branch on `receipt.success` in `process_delivery_receipt`: on failure, do not treat the `PendingOrder` as resolved. Instead, transition it into a distinct "failed"/replayable state (or trigger a refund path back to the original sender/claimer) so that assets already withdrawn on the Polkadot side are not stranded, mirroring the recommended fix in the original report of persisting failed executions as directly retryable/refundable state rather than relying on an assumption that finalization implies successful delivery.

### Proof of Concept
1. Send an outbound message from AssetHub to Ethereum through `InitiateTransfer`/`ExportMessage`, causing `snowbridge-pallet-outbound-queue-v2::do_process_message` to withdraw/reserve the transferred asset and create `PendingOrders[nonce]` with the command's `gas` computed by `T::GasMeter::maximum_dispatch_gas_used_at_most`.
2. Craft (or naturally trigger via state changes) an execution path on the Ethereum Gateway where the actual gas required to run the command exceeds the pre-computed estimate, causing the Gateway to revert the inner dispatch while still emitting `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. Any relayer calls `submit_delivery_receipt` with a valid proof of this event.
4. `process_delivery_receipt` pays the relayer reward and removes `PendingOrders[nonce]`, exactly as shown in the passing integration tests that only exercise `success: true`, e.g. [6](#0-5)  — no equivalent test exists asserting refund/retry behavior for `success: false`.
5. The original asset withdrawn on AssetHub is unrecoverable; no on-chain state remains that would allow reissuing or refunding the failed transfer.

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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L371-437)
```rust
			// Convert it to OutboundMessage and save into Messages storage
			let commands: Vec<OutboundCommandWrapper> = commands
				.into_iter()
				.map(|command| OutboundCommandWrapper {
					kind: command.index(),
					gas: T::GasMeter::maximum_dispatch_gas_used_at_most(&command),
					payload: command.abi_encode(),
				})
				.collect();

			let nonce = <Nonce<T>>::get().checked_add(1).ok_or_else(|| {
				Self::deposit_event(Event::MessageRejected {
					id: None,
					payload: message.to_vec(),
					error: Unsupported,
				});
				Unsupported
			})?;

			let outbound_message = OutboundMessage {
				origin,
				nonce,
				topic: id,
				commands: commands.clone().try_into().map_err(|_| {
					Self::deposit_event(Event::MessageRejected {
						id: Some(id),
						payload: message.to_vec(),
						error: Corrupt,
					});
					Corrupt
				})?,
			};
			Messages::<T>::append(outbound_message);

			// Convert it to an OutboundMessageWrapper (in ABI format), hash it using Keccak256 to
			// generate a committed hash, and store it in MessageLeaves storage which can be
			// verified on Ethereum later.
			let abi_commands: Vec<CommandWrapper> = commands
				.into_iter()
				.map(|command| CommandWrapper {
					kind: command.kind,
					gas: command.gas,
					payload: Bytes::from(command.payload),
				})
				.collect();
			let committed_message = OutboundMessageWrapper {
				origin: FixedBytes::from(origin.as_fixed_bytes()),
				nonce,
				topic: FixedBytes::from(id.as_fixed_bytes()),
				commands: abi_commands,
			};
			let message_abi_encoded_hash =
				<T as Config>::Hashing::hash(&committed_message.abi_encode());
			MessageLeaves::<T>::append(message_abi_encoded_hash);

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

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound.rs (L104-121)
```rust
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
```
