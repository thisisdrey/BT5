## Analysis

The Velodrome report's core broken invariant is: **a public entrypoint lets the caller decide the fee/value attached to an outbound cross-chain message, and the protocol commits to sending that message before checking the supplied amount is sufficient for the message to actually be delivered/executed** — resulting in stuck or lost value.

The closest local analog is in the Snowbridge **outbound-queue-v2** pallet's message-commit path, on BridgeHub.

### Where the analog lives

`Pallet::do_process_message` decodes an already-enqueued `Message` — including a caller-influenced `fee` field — and unconditionally records it as the relayer reward for that nonce, with no check against the actual gas/execution cost required to deliver the message to Ethereum: [1](#0-0) 

The commands' real, on-chain-computed gas cost is calculated right there via `T::GasMeter::maximum_dispatch_gas_used_at_most`, but that computed value is only stored per-command for the Ethereum-side gas limit — it is never compared to `fee` before the `PendingOrder` is created: [2](#0-1) 

The nonce is advanced and the message is irrevocably committed to `Messages`/`MessageLeaves` (i.e. queued for merkle-root commitment and eventual relay to Ethereum) with whatever `fee` was decoded, including `0`: [3](#0-2) 

Delivery reward payment later branches explicitly on `order.fee > 0`, confirming the pallet accepts and persists a zero or arbitrarily low fee as valid: [4](#0-3) 

### Title
Outbound queue v2 commits Ethereum-bound messages with unvalidated relayer fee, allowing permanently stalled bridge delivery - (File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs)

### Summary
`do_process_message` accepts a `fee` value carried inside the enqueued `Message` payload and stores it as-is in `PendingOrders`, without validating it against the gas cost `T::GasMeter` computes for the same message. Because relayers are only economically incentivized by `order.fee` (paid via `T::RewardPayment::register_reward` in `process_delivery_receipt`), a message committed with `fee == 0` or a fee far below the real Ethereum gas cost has no rational relayer to submit `submit_delivery_receipt` for it.

### Finding Description
This mirrors the Velodrome finding exactly at the invariant level: a message destined for cross-chain execution is dispatched with a caller-supplied value that is never checked against the actual required cost, so the message can be sent knowing it will not be completed. Here, `fee` originates upstream from the XCM conversion pipeline (`snowbridge_outbound_queue_primitives::v2` converter/exporter, ultimately reachable from ordinary user XCM such as `InitiateAssetsTransfer`/`ExchangeAsset` reward legs described in `bridges/snowbridge/docs/v2.md`) and is decoded verbatim in `do_process_message`. Unlike v1 (`bridges/snowbridge/pallets/outbound-queue/src/send_message_impl.rs`), which computes `fee` itself via `Self::calculate_fee(gas_used_at_most, T::PricingParameters::get())` at `validate()` time, the v2 pallet's `SendMessage::validate` in `send_message_impl.rs` does not compute or validate a fee at all — it only checks payload size: [5](#0-4) 

The fee is instead embedded by the caller/converter in the `Message` and only surfaces again at `do_process_message`, where it is committed to chain state unconditionally.

### Impact Explanation
A message with an underpriced (including zero) fee still consumes a nonce, occupies a slot in `MaxMessagesPerBlock`, is committed into the merkle root (`commit()`), and is presented on Ethereum for verification — but no relayer will spend gas submitting `submit_delivery_receipt` for a reward that doesn't cover their cost. The message and any associated cross-chain intent (e.g., assets already reserved/burned on the Polkadot side as part of the P→E transfer flow) become permanently stuck: `PendingOrders` entries are never resolved, matching the "permanent user-fund or bridge-state lock" and "public underpriced work that... stalls bridge processing" impact categories.

### Likelihood Explanation
This requires no privileged actor, relayer collusion, or governance action — any account able to originate a Snowbridge V2 XCM transfer (via `EthereumBlobExporter::deliver` or `snowbridge_pallet_system_v2::Pallet::send`) controls the fee/reward amount that ends up in the `Message`. Supplying an insufficient value is a normal, unprivileged user mistake or a griefing action, and the pallet provides no minimum-fee enforcement at the point of commitment.

### Recommendation
In `do_process_message` (or earlier in `send_message_impl::validate`), compute the minimum acceptable fee/reward using `T::GasMeter::maximum_gas_used_at_most`/pricing parameters (as v1's `calculate_fee` does) and reject or hold messages whose supplied `fee` is below that computed minimum, rather than committing them unconditionally.

### Proof of Concept
1. Construct a Snowbridge V2 XCM transfer from a sibling parachain/AssetHub whose `Message.fee` (the WETH reward leg) is set to `0` or a negligible amount, using `EthereumBlobExporter::deliver`/`snowbridge_pallet_system_v2::Pallet::send`.
2. The message reaches `Pallet::do_process_message` at [6](#0-5) , is accepted, assigned a nonce, and stored in `PendingOrders` with `fee = 0`.
3. The message is committed into the merkle root and becomes visible to Ethereum-side relayers, but with `order.fee == 0` no relayer executes `submit_delivery_receipt`, since `process_delivery_receipt` only pays a reward `if order.fee > 0` (`lib.rs:466-473`).
4. The message remains permanently in `PendingOrders`, never delivered, while any assets consumed on the source side for the transfer are already gone — reproducing the report's "insufficient fee for cross-chain delivery causes stuck/failed message," but with unbacked state lock instead of a simple reverted transaction.

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L360-369)
```rust
			// Decode bytes into Message
			let Message { origin, id, fee, commands } =
				Message::decode(&mut message).map_err(|_| {
					Self::deposit_event(Event::MessageRejected {
						id: None,
						payload: message.to_vec(),
						error: Corrupt,
					});
					Corrupt
				})?;
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L371-379)
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
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L426-443)
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

			Ok(true)
		}
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L462-473)
```rust
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
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/send_message_impl.rs (L23-32)
```rust
	fn validate(message: &Message) -> Result<Self::Ticket, SendError> {
		// The inner payload should not be too large
		let payload = message.encode();
		ensure!(
			payload.len() < T::MaxMessagePayloadSize::get() as usize,
			SendError::MessageTooLarge
		);

		Ok(message.clone())
	}
```
