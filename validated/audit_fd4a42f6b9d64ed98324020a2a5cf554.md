## Title
Zero-fee outbound message spam floods the BridgeHub outbound-queue-v2 / MessageQueue, DoS-ing Ethereum-bound relayer delivery - ([File: bridges/snowbridge/pallets/outbound-queue-v2/src/send_message_impl.rs])

### Summary
The Snowbridge v2 outbound pipeline (`SendMessage::validate`/`deliver` in `bridges/snowbridge/pallets/outbound-queue-v2/src/send_message_impl.rs`) enqueues any XCM-derived message into `pallet-message-queue` without validating that `Message.fee` is non-zero or sufficient. This is the same broken invariant as the reported bug: cheap/free requests can be submitted without bound, filling the processing queue (`MaxMessagesPerBlock` slots per block, `MessageQueue`/`PendingOrders` storage) and starving legitimate users' messages, causing indefinite delay of processing and of the relayer reward mechanism.

### Finding Description
`snowbridge_pallet_outbound_queue_v2`'s `SendMessage::validate` only checks the payload size: [1](#0-0) 

`deliver` unconditionally enqueues the message into `T::MessageQueue` and emits `MessageQueued`, with no fee sufficiency check: [2](#0-1) 

The `fee` field embedded in the `Message` is derived from the XCM `PayFees` instruction amount via `XcmConverter::extract_remote_fee`, which is fully attacker-controlled through the XCM the user submits (e.g. via AssetHub's `ExportMessage`). Nothing in `convert.rs` enforces a minimum non-zero fee value — the design doc for v2 explicitly acknowledges this gap and only recommends (not enforces) "a minimum relayer reward of at least the existential deposit 0.1 DOT... to stop spamming messages with 0 rewards" (`bridges/snowbridge/docs/v2.md` lines 99-102), confirming this protection is not actually implemented in the pallet logic.

Once enqueued, `do_process_message` in `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs` processes up to `MaxMessagesPerBlock` messages per block regardless of fee, creating a `PendingOrder` with the attacker-controlled (potentially zero) `fee`: [3](#0-2) 

Because `MaxMessagesPerBlock` limits per-block committed messages and `MessageLeaves`/`Messages` storage growth, and because the underlying `pallet-message-queue` services queues FIFO per origin (`service_queue`), an attacker who repeatedly submits maximum-size zero-fee messages from a cheap/low-friction origin (e.g. AssetHub via XCM, or directly through `snowbridge_pallet_system_v2::Pallet::send`) can occupy the `MaxMessagesPerBlock` cap every block, delaying legitimate paying users' messages from ever being committed and relayed — mirroring exactly the external report's "relayer must act on illegitimate cheap messages first" DoS pattern, except here the bottleneck is the on-chain commitment/merkle-root pipeline and relayer economic incentive (zero reward messages are unprofitable for relayers to service, so they will be skipped/delayed, compounding the backlog in `PendingOrders`).

### Impact Explanation
This matches the "public underpriced work that degrades block production or stalls bridge processing" impact category. An attacker can:
1. Submit a continuous stream of maximum-payload, zero/near-zero-fee messages through the v2 exporter/system pallet.
2. Saturate `MaxMessagesPerBlock` every block, causing genuine, fee-paying user messages to be `MessagePostponed` (yielded) indefinitely.
3. Create unbounded growth in `PendingOrders` storage (only removed on `submit_delivery_receipt`, which relayers have no economic incentive to do for zero-fee orders), degrading BridgeHub state and stalling the Ethereum delivery pipeline for legitimate users.

### Likelihood Explanation
The attack requires only an ordinary, unprivileged user able to submit XCM `ExportMessage`/`PayFees` instructions with an attacker-chosen (zero) fee amount, or to call `snowbridge_pallet_system_v2::Pallet::send` with `fee: 0`. No malicious relayer, validator, governance, or leaked key is needed — it is a pure public-entrypoint underpriced-work issue, satisfying the required-impact gate.

### Recommendation
- Enforce a minimum non-zero fee/reward in `XcmConverter::extract_remote_fee` (`convert.rs`) and/or in `SendMessage::validate` (`send_message_impl.rs`), rejecting messages whose `fee` is below a configured `MinimumReward`/existential-deposit-equivalent threshold, as already flagged as a design intention in `bridges/snowbridge/docs/v2.md`.
- Consider per-origin rate limiting or fee-weighted queue admission (e.g. priority queues by fee) in `pallet-message-queue`/outbound-queue-v2 admission logic, analogous to the DMP dynamic fee-factor mechanism (`polkadot/runtime/parachains/src/dmp.rs`) that increases delivery cost as queue occupancy grows.
- Bound/expire stale zero-fee `PendingOrders` to prevent unbounded storage growth.

### Proof of Concept
1. On AssetHub, construct an XCM message routed to the Snowbridge v2 exporter containing:
```
WithdrawAsset(ETH, 0)
PayFees { asset: ETH, amount: 0 }
WithdrawAsset(<some ENA>, 1)
AliasOrigin(attacker)
DepositAsset(...)
SetTopic(...)
```
2. `XcmConverter::extract_remote_fee` accepts `amount = 0` (no minimum check in `convert.rs`), producing `Message { fee: 0, .. }`.
3. `EthereumBlobExporter::validate` calls `OutboundQueue::validate` (passes, only size-checked) then the message is delivered/enqueued (`send_message_impl.rs`).
4. Repeat submission in a loop each block up to `MaxMessagesPerBlock` times; `do_process_message` commits these zero-fee messages ahead of/alongside legitimate ones, filling the block's commitment capacity and causing genuine messages to receive `MessagePostponed` events, delaying their processing and relayer settlement indefinitely.

### Citations

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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/send_message_impl.rs (L34-43)
```rust
	fn deliver(ticket: Self::Ticket) -> Result<H256, SendError> {
		let origin = ticket.origin.into();

		let message =
			BoundedVec::try_from(ticket.encode()).map_err(|_| SendError::MessageTooLarge)?;

		T::MessageQueue::enqueue_message(message.as_bounded_slice(), origin);
		Self::deposit_event(Event::MessageQueued { message: ticket.clone() });
		Ok(ticket.id)
	}
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L343-443)
```rust
		pub(crate) fn do_process_message(
			_: ProcessMessageOriginOf<T>,
			mut message: &[u8],
		) -> Result<bool, ProcessMessageError> {
			use ProcessMessageError::*;

			// Yield if the maximum number of messages has been processed this block.
			// This ensures that the weight of `on_finalize` has a known maximum bound.
			let current_len = MessageLeaves::<T>::decode_len().unwrap_or(0);
			if current_len >= T::MaxMessagesPerBlock::get() as usize {
				Self::deposit_event(Event::MessagePostponed {
					payload: message.to_vec(),
					reason: Yield,
				});
				return Err(Yield);
			}

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

			<Nonce<T>>::set(nonce);

			Self::deposit_event(Event::MessageAccepted { id, nonce });

			Ok(true)
		}
```
