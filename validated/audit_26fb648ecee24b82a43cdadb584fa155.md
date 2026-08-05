Confirmed. `process_delivery_receipt` in `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs` never references `receipt.success` — a grep across the whole file for `success` returns zero matches, and the function body only checks `receipt.gateway`, looks up `PendingOrders`, pays the reward, and unconditionally removes the order.Audit Report

## Title
`process_delivery_receipt` finalizes relayer reward and permanently deletes `PendingOrders` state without checking the `DeliveryReceipt.success` flag - (File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs)

## Summary
`Pallet::process_delivery_receipt` decodes a `DeliveryReceipt` that carries a `success` field reflecting whether the Ethereum Gateway's `InboundMessageDispatched` execution actually succeeded, but the function body never inspects `receipt.success` — a full-file `success` grep on `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs` returns zero matches. It unconditionally pays the relayer reward and calls `<PendingOrders<T>>::remove(nonce)`, the only storage tracking that message's outcome, regardless of whether the bridged command actually executed on Ethereum.

## Finding Description
The lifecycle: `do_process_message` computes a static worst-case gas budget per command via `T::GasMeter::maximum_dispatch_gas_used_at_most(&command)` (implemented by `ConstantGasMeter`, using constants such as `UnlockNativeToken => 200_000`, `MintForeignToken => 100_000`, or the caller-supplied `gas_limit` for `CallContract`) and stores a `PendingOrder{ nonce, fee, block_number }` keyed by nonce [1](#0-0) [2](#0-1) .

The Ethereum Gateway emits `InboundMessageDispatched(nonce, topic, success, reward_address)`, which `DeliveryReceipt::try_from` decodes into a `success: bool` field <cite repo="ThankGodontt/polkadot-sdk--028" path="bridges/snowbridge/primitives/outbound-queue/src/v2/delivery_receipt.rs" start="10-27" end="10-27" />.

`process_delivery_receipt` only validates `receipt.gateway`, fetches the `PendingOrder`, pays `order.fee` to the relayer via `T::RewardPayment::register_reward`, and unconditionally removes the order — `receipt.success` is decoded but never branched on: [3](#0-2) 

There is no other storage item in this pallet (or elsewhere in the searched Snowbridge outbound pipeline) that records a failed dispatch for retry, refund, or replay; once `PendingOrders::remove(nonce)` executes the message's fate is permanently sealed, with only a `MessageDelivered` event emitted regardless of `success`.

## Impact Explanation
This matches the "permanent user-fund or bridge-state lock" impact category in the required-impacts gate. Commands such as `UnlockNativeToken` and `MintForeignToken` move value on the Ethereum side; corresponding assets/allowances on the Polkadot side are committed at message-enqueue time via the `Message`/`Command` pipeline. If the Gateway's dispatch of the command reverts on Ethereum (e.g., due to `maximum_dispatch_gas_used_at_most` underestimating real gas cost, which is explicitly a heuristic/worst-case constant table, not a guaranteed bound), the Gateway still emits `InboundMessageDispatched` with `success=false`. Because `process_delivery_receipt` ignores this flag, the relayer is paid and the `PendingOrder` is deleted as if delivery succeeded, permanently foreclosing any retry/refund path for the underlying value transfer.

## Likelihood Explanation
No malicious relayer, governance, or admin action is required. Any relayer submitting a truthful, cryptographically verified proof of a real `InboundMessageDispatched(success=false)` event will trigger this path deterministically through the public `submit_delivery_receipt` extrinsic [4](#0-3) . This is reachable purely from the gas-estimation heuristic in `ConstantGasMeter` being wrong for a given execution path, or from adversarial command construction that guarantees reversion after fee-relevant state changes on Ethereum.

I was unable to fully trace, within the available tooling and time, the exact upstream code that withdraws/reserves assets on AssetHub before message enqueue (e.g., the `EthereumBlobExporter`/XCM converter logic referenced in the original report), so I cannot independently confirm the precise mechanics of "funds withdrawn at enqueue time" beyond what's stated in the original report; however, the core claim — that `process_delivery_receipt` never checks `receipt.success` before finalizing reward and removing `PendingOrders` — is directly confirmed in this repository's code.

## Recommendation
Branch on `receipt.success` in `process_delivery_receipt`. On failure, do not treat the `PendingOrder` as resolved via unconditional removal; instead transition it to a distinct failed/replayable state, or trigger an on-chain refund path back to the original sender, so that value already committed on the Polkadot side is not permanently stranded when the Ethereum-side dispatch fails.

## Proof of Concept
1. Enqueue an outbound message containing an `UnlockNativeToken` or `MintForeignToken` command; `do_process_message` computes `gas` via `ConstantGasMeter::maximum_dispatch_gas_used_at_most` and inserts `PendingOrders[nonce]` [5](#0-4) .
2. On Ethereum, the Gateway's dispatch of the command reverts (gas underestimate or crafted target) while still emitting `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. A relayer calls `submit_delivery_receipt` with a valid proof of this event; `T::Verifier::verify` succeeds and `DeliveryReceipt::try_from` decodes `success: false` [6](#0-5) .
4. `process_delivery_receipt` pays the relayer reward and calls `<PendingOrders<T>>::remove(nonce)` unconditionally, since `receipt.success` is never checked [7](#0-6) .
5. No storage state remains that would allow retry, refund, or replay of the failed command; a unit test constructing a `DeliveryReceipt{ success: false, .. }` and asserting `PendingOrders` still exists (or a refund path fires) after calling `process_delivery_receipt` would demonstrate the bug, mirroring the existing `success: true` test path in the emulated integration tests.

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

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/message.rs (L291-306)
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
}
```
