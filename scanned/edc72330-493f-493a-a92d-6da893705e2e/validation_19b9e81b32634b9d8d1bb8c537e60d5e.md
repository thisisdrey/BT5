## Analysis Result

The `DeliveryReceipt.success` field decoded from Ethereum's `InboundMessageDispatched` event is **never checked** by `process_delivery_receipt`. The relayer reward is paid, and the `PendingOrder` is removed, purely based on `order.fee > 0` and a valid nonce/gateway match — regardless of whether the message execution on Ethereum actually succeeded. [1](#0-0) [2](#0-1) 

### Title
Snowbridge outbound-queue-v2 settles pending orders and pays relayer rewards without checking delivery `success` — ([File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs])

### Summary
This is the closest local analog to the Optimism `l2Gas` underpayment bug. In the external report, an under-specified gas parameter causes the cross-chain call to fail on the destination, while the source-side accounting proceeds as if delivery succeeded, risking fund loss. In Snowbridge's outbound-queue-v2, the `Command::CallContract` variant lets the *sender* supply an arbitrary `gas` value [3](#0-2)  which becomes the on-chain-committed `max_dispatch_gas` via `ConstantGasMeter::maximum_dispatch_gas_used_at_most` [4](#0-3) . If that gas is insufficient, execution of the command on the Gateway contract on Ethereum fails, and the Gateway emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.

However, `process_delivery_receipt` — the extrinsic that settles the `PendingOrder` for that nonce and pays the relayer reward — decodes this event into a `DeliveryReceipt` that includes the `success` flag [5](#0-4) , but the pallet logic never reads or branches on `receipt.success`:

```rust
pub fn process_delivery_receipt(...) -> DispatchResult {
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
``` [6](#0-5) 

### Finding Description
The comment header in the same file documents the intended flow: "Fetch the pending order by nonce of the message, pay reward with fee attached in the order," with no mention of gating on execution outcome [7](#0-6) . The `success` bit exists specifically to communicate whether the dispatched `Command` (e.g. `CallContract`, `UnlockNativeToken`, `MintForeignToken`) actually executed, yet nothing in `process_delivery_receipt` consumes it — the reward is paid and the order is finalized (`PendingOrders::remove(nonce)`) unconditionally once a valid receipt for that nonce/gateway arrives.

The all-emulated test suite consistently constructs `DeliveryReceipt { success: true, .. }` and never exercises `success: false`, confirming that the `false` path is untested and unhandled [8](#0-7) .

This directly mirrors the underpaid-`l2Gas` root cause: a caller-controlled gas value (`Command::CallContract { gas, .. }`) can make execution fail on the destination chain, but the source-chain ledger (here: `PendingOrders` state and relayer reward payout) advances and settles as if delivery succeeded, because the emitted "did it actually succeed" signal is discarded.

### Impact Explanation
This breaks the pallet's own documented invariant that "message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically." A relayer is paid `order.fee` (potentially non-trivial ether) and the order is permanently cleared even for a failed dispatch (e.g. `CallContract` with insufficient `gas`, or any command that reverts on Ethereum for other reasons). This is a public underpriced/incorrect-settlement condition: value (the reward) is paid out for work that did not achieve its intended effect, and the accounting state (`PendingOrders`) is destroyed so there is no way to retry, dispute, or re-settle the failed command. Any user routing funds/commands through `CallContract` with attacker- or misconfigured-`gas` risks the command failing on Ethereum while BridgeHub still treats it as delivered and pays the relayer — a duplicate/incorrect settlement, distinct from and unguarded by any existing check (`GatewayAddress` match and nonce lookup are the only gates, neither of which validate `success`).

### Likelihood Explanation
No malicious peer, relayer, validator, or governance actor is required. Any user constructing a v2 XCM `Transact`/`ContractCall::V1` message with `CallContract` (or triggering any other command) can supply/trigger a low `gas` value or otherwise cause on-chain revert; a normal, honest relayer merely submits the resulting `InboundMessageDispatched` event (with `success=false`) as proof, and the pallet's existing code path pays out and clears the order regardless. This is a straightforward public-entrypoint path (`submit_delivery_receipt` is open to `ensure_signed` callers) with no additional privilege needed.

### Recommendation
`process_delivery_receipt` should branch on `receipt.success`: only pay the relayer reward and clear the `PendingOrder` on `success == true`. On `success == false`, either (a) retain the order for retry/reprocessing without paying the fee, or (b) transition to an explicit failure-handling path (e.g. refund the fee to the original sender, emit a distinct `MessageDeliveryFailed` event) rather than silently discarding the failure signal that the Ethereum contract deliberately emits.

### Proof of Concept
1. User submits a v2 XCM message containing `Command::CallContract { target, calldata, gas: <too_low>, value }` via the `AliasOrigin`/`Transact` flow in `XcmConverter::convert` [9](#0-8) .
2. `do_process_message` commits the `OutboundCommandWrapper` with `gas` taken verbatim from the message and enqueues a `PendingOrder{nonce, fee, ..}` [10](#0-9) .
3. On Ethereum, the Gateway executes the command with the underpaid `gas`, the inner call reverts/out-of-gas, and the Gateway emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
4. A relayer (honest, no collusion needed) submits this event log via `submit_delivery_receipt`; `T::Verifier::verify` succeeds (the event itself is real and correctly proven), `DeliveryReceipt::try_from` decodes `success=false` correctly.
5. `process_delivery_receipt` ignores `success`, pays `order.fee` to `reward_account`, and removes the `PendingOrder`, emitting `Event::MessageDelivered` — despite the underlying command never having taken effect on Ethereum.

I could not verify from the available index whether any downstream consumer (e.g. `snowbridge-pallet-system-v2`) separately reconciles failed commands using the `success` flag from a different path; if no such reconciliation exists, the failure is permanently unrecoverable in-protocol.

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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L371-436)
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

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/message.rs (L182-192)
```rust
	/// Call Contract on Ethereum
	CallContract {
		/// Target contract address
		target: H160,
		/// ABI-encoded calldata
		calldata: Vec<u8>,
		/// Maximum gas to forward to target contract
		gas: u64,
		/// Include ether held by agent contract
		value: u128,
	},
```

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/message.rs (L291-305)
```rust
impl GasMeter for ConstantGasMeter {
	fn maximum_dispatch_gas_used_at_most(command: &Command) -> u64 {
		match command {
			Command::SetOperatingMode { .. } => 40_000,
			Command::Upgrade { initializer, .. } => {
				// total maximum gas must also include the gas used for updating the proxy before
				// the the initializer is called.
				50_000 + initializer.maximum_required_gas
			},
			Command::UnlockNativeToken { .. } => 200_000,
			Command::RegisterForeignToken { .. } => 1_200_000,
			Command::MintForeignToken { .. } => 100_000,
			Command::CallContract { gas: gas_limit, .. } => *gas_limit,
		}
	}
```

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound.rs (L409-418)
```rust
		let receipt = DeliveryReceipt {
			gateway: EthereumGatewayAddress::get(),
			nonce: 1,
			reward_address: reward_account.into(),
			topic: H256::zero(),
			success: true,
		};

		// Submit a delivery receipt
		assert_ok!(EthereumOutboundQueueV2::process_delivery_receipt(relayer, receipt));
```

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs (L294-305)
```rust
		// Transact commands
		let transact_call = match_expression!(self.peek(), Ok(Transact { call, .. }), call);
		if let Some(transact_call) = transact_call {
			let _ = self.next();
			let transact =
				ContractCall::decode_all(&mut transact_call.clone().into_encoded().as_slice())
					.map_err(|_| TransactDecodeFailed)?;
			match transact {
				ContractCall::V1 { target, calldata, gas, value } => commands
					.push(Command::CallContract { target: target.into(), calldata, gas, value }),
			}
		}
```
