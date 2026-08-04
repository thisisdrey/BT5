## Analysis

**Core broken invariant in the external report:** once a bad root/message is accepted into a queue, there is no owner/root-controlled mechanism to strike it before it propagates to the destination chain, because "pausing" the contract does not retroactively purge already-accepted queue entries.

**Local analog found:** Snowbridge's outbound-queue pallet (`snowbridge-pallet-outbound-queue`) exhibits the same gap. `set_operating_mode` is the only administrative lever exposed, and it is checked exclusively at the enqueue boundary in `deliver()`. The actual processing/commitment path, `do_process_message`, contains **no** check of `OperatingMode`/halted state at all, and there is no extrinsic to remove a specific message already sitting in the `MessageQueue` pallet's storage. So a message that is already inside the underlying `pallet-message-queue` queue at the moment Root discovers a problem and calls `set_operating_mode(Halted)` will still be picked up by `ProcessMessage::process_message` → `do_process_message`, appended to `Messages`/`MessageLeaves`, and merkle-committed into the parachain header digest via `commit()` in the very next `on_finalize`, with no way for the owner to excise it first.

### Title
Outbound-queue pallet cannot remove or pause already-enqueued messages before Ethereum commitment - (`bridges/snowbridge/pallets/outbound-queue/src/lib.rs`)

### Summary
`set_operating_mode` (Root-only) is meant to halt the outbound bridge pipeline, but the halted check is only enforced in `deliver()` at enqueue time [1](#0-0) . The processing/commit path, `do_process_message`, never checks `OperatingMode`, so any message already sitting in `T::MessageQueue`'s storage at halt time is still decoded, given a nonce, appended to `Messages`, hashed into `MessageLeaves`, and later folded into the Ethereum-bound merkle root by `commit()` [2](#0-1) [3](#0-2) . There is no owner/root-callable method to purge a specific offending message by content/nonce from the queue before it is committed — the only removal primitives in the underlying `pallet-message-queue` are `reap_page` (only for fully-processed/empty pages) and `execute_overweight` (only for permanently-overweight messages), neither of which lets an owner excise a specific, still-processable message [4](#0-3) .

### Finding Description
`OperatingMode` is a `StorageValue<BasicOperatingMode>` set only by `set_operating_mode`, Root-gated [5](#0-4) . It is read in exactly one place — `deliver()` — to block **new** enqueues for non-governance channels [1](#0-0) . Messages already sitting inside the generic `T::MessageQueue` (a `pallet-message-queue` instance) are serviced on every block by that pallet's own weight-bounded scheduler, invoking `ProcessMessage::process_message` → `do_process_message` regardless of the outbound-queue pallet's `OperatingMode` value, because `do_process_message` only checks `MaxMessagesPerBlock` (Yield) and payload decode/size errors (Corrupt/Unsupported) — never `OperatingMode` [6](#0-5) . Once processed, the message is irreversibly appended into `Messages` and `MessageLeaves`, and at `on_finalize` the commit function folds all leaves into a merkle root that is written into the header digest and exposed to Ethereum-side relayers/light clients via `prove_message` [3](#0-2) . The identical structure (no halted gate inside `do_process_message`, no admin removal of a queued-but-not-yet-committed message) is repeated in the v2 outbound queue pallet [7](#0-6) .

### Impact Explanation
If a message is enqueued that later turns out to be fraudulent/corrupted state (e.g. a bug in an upstream XCM exporter or a misconfigured sending pallet produces a bad `command`/payload that still decodes and fits size limits), Root's only defensive tool — `set_operating_mode(Halted)` — does not stop it from being committed to the merkle root and exposed to Ethereum relayers within the same or next block. This is a public-underpriced-work / bridge-processing-integrity impact: the chain cannot stop propagation of an already-accepted bad message to the bridge destination once halted, mirroring exactly the reported Connext defect ("fraudulent roots cannot be removed... will be propagated to each chain").

### Likelihood Explanation
This does not require a malicious peer, validator, or admin abuse — it is a structural gap reachable purely by Root exercising its documented "halt" capability after any bad message has already entered the message-queue storage (which can happen from ordinary send-message traffic, e.g. `EthereumBlobExporter::deliver` or `snowbridge_pallet_system::Pallet::send`), before Root becomes aware and halts. The likelihood of "message already queued when problem is detected" is inherent to any queue with async, weight-bounded servicing — the window is real and not attacker-controlled.

### Recommendation
Add an `OperatingMode`/halted check inside `do_process_message` (return `ProcessMessageError::Yield` or a new permanent-skip variant when halted) so that halting genuinely freezes commitment of queued messages, and add a Root-only extrinsic to purge a specific enqueued-but-unprocessed message (by origin/page/index, similar in spirit to `execute_overweight`/`reap_page` in `pallet-message-queue`) so a detected-bad message can be struck out of the pipeline before it is folded into `MessageLeaves` and committed to the header digest.

### Proof of Concept
1. A message is enqueued via `deliver()` while `OperatingMode` is `Normal`, landing in `T::MessageQueue` storage.
2. Before the block ends, Root discovers the message is malformed/fraudulent and calls `set_operating_mode(Halted)`.
3. In the same or a subsequent block, `pallet-message-queue`'s scheduler still invokes `Pallet::do_process_message` for the pending message (no halted gate exists there) [6](#0-5) .
4. The message is appended to `Messages`/`MessageLeaves` and, at `on_finalize`, `commit()` builds the merkle root and writes it to the header digest, making the bad message provable and relayable to Ethereum [3](#0-2) .
5. No extrinsic exists in this pallet, or in the generic `pallet-message-queue` (`reap_page`/`execute_overweight` only) [4](#0-3) , that lets Root remove that specific message between steps 2 and 3.

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue/src/send_message_impl.rs (L76-88)
```rust
	fn deliver(ticket: Self::Ticket) -> Result<H256, SendError> {
		let origin = AggregateMessageOrigin::Snowbridge(ticket.channel_id);

		if ticket.channel_id != PRIMARY_GOVERNANCE_CHANNEL {
			ensure!(!Self::operating_mode().is_halted(), SendError::Halted);
		}

		let message = ticket.message.as_bounded_slice();

		T::MessageQueue::enqueue_message(message, origin);
		Self::deposit_event(Event::MessageQueued { id: ticket.message_id });
		Ok(ticket.message_id)
	}
```

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L265-278)
```rust
	#[pallet::call]
	impl<T: Config> Pallet<T> {
		/// Halt or resume all pallet operations. May only be called by root.
		#[pallet::call_index(0)]
		#[pallet::weight((T::DbWeight::get().reads_writes(1, 1), DispatchClass::Operational))]
		pub fn set_operating_mode(
			origin: OriginFor<T>,
			mode: BasicOperatingMode,
		) -> DispatchResult {
			ensure_root(origin)?;
			OperatingMode::<T>::put(mode);
			Self::deposit_event(Event::OperatingModeChanged { mode });
			Ok(())
		}
```

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L281-298)
```rust
	impl<T: Config> Pallet<T> {
		/// Generate a messages commitment and insert it into the header digest
		pub(crate) fn commit() {
			let count = MessageLeaves::<T>::decode_len().unwrap_or_default() as u64;
			if count == 0 {
				return;
			}

			// Create merkle root of messages
			let root = merkle_root::<<T as Config>::Hashing, _>(MessageLeaves::<T>::stream_iter());

			let digest_item: DigestItem = SnowbridgeDigestItem::Snowbridge(root).into();

			// Insert merkle root into the header digest
			<frame_system::Pallet<T>>::deposit_log(digest_item);

			Self::deposit_event(Event::MessagesCommitted { root, count });
		}
```

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L300-364)
```rust
		/// Process a message delivered by the MessageQueue pallet
		pub(crate) fn do_process_message(
			_: ProcessMessageOriginOf<T>,
			mut message: &[u8],
		) -> Result<bool, ProcessMessageError> {
			use ProcessMessageError::*;

			// Yield if the maximum number of messages has been processed this block.
			// This ensures that the weight of `on_finalize` has a known maximum bound.
			ensure!(
				MessageLeaves::<T>::decode_len().unwrap_or(0) <
					T::MaxMessagesPerBlock::get() as usize,
				Yield
			);

			// Decode bytes into versioned message
			let versioned_queued_message: VersionedQueuedMessage =
				VersionedQueuedMessage::decode(&mut message).map_err(|_| Corrupt)?;

			// Convert versioned message into latest supported message version
			let queued_message: QueuedMessage =
				versioned_queued_message.try_into().map_err(|_| Unsupported)?;

			// Obtain next nonce
			let nonce = <Nonce<T>>::try_mutate(
				queued_message.channel_id,
				|nonce| -> Result<u64, ProcessMessageError> {
					*nonce = nonce.checked_add(1).ok_or(Unsupported)?;
					Ok(*nonce)
				},
			)?;

			let pricing_params = T::PricingParameters::get();
			let command = queued_message.command.index();
			let params = queued_message.command.abi_encode();
			let max_dispatch_gas =
				T::GasMeter::maximum_dispatch_gas_used_at_most(&queued_message.command);
			let reward = pricing_params.rewards.remote;

			// Construct the final committed message
			let message = CommittedMessage {
				channel_id: queued_message.channel_id,
				nonce,
				command,
				params,
				max_dispatch_gas,
				max_fee_per_gas: pricing_params
					.fee_per_gas
					.try_into()
					.defensive_unwrap_or(u128::MAX),
				reward: reward.try_into().defensive_unwrap_or(u128::MAX),
				id: queued_message.id,
			};

			// ABI-encode and hash the prepared message
			let message_abi_encoded = ethabi::encode(&[message.clone().into()]);
			let message_abi_encoded_hash = <T as Config>::Hashing::hash(&message_abi_encoded);

			Messages::<T>::append(Box::new(message));
			MessageLeaves::<T>::append(message_abi_encoded_hash);

			Self::deposit_event(Event::MessageAccepted { id: queued_message.id, nonce });

			Ok(true)
		}
```

**File:** substrate/frame/message-queue/src/lib.rs (L714-757)
```rust
	#[pallet::call]
	impl<T: Config> Pallet<T> {
		/// Remove a page which has no more messages remaining to be processed or is stale.
		#[pallet::call_index(0)]
		#[pallet::weight(T::WeightInfo::reap_page())]
		pub fn reap_page(
			origin: OriginFor<T>,
			message_origin: MessageOriginOf<T>,
			page_index: PageIndex,
		) -> DispatchResult {
			ensure_signed(origin)?;
			Self::do_reap_page(&message_origin, page_index)
		}

		/// Execute an overweight message.
		///
		/// Temporary processing errors will be propagated whereas permanent errors are treated
		/// as success condition.
		///
		/// - `origin`: Must be `Signed`.
		/// - `message_origin`: The origin from which the message to be executed arrived.
		/// - `page`: The page in the queue in which the message to be executed is sitting.
		/// - `index`: The index into the queue of the message to be executed.
		/// - `weight_limit`: The maximum amount of weight allowed to be consumed in the execution
		///   of the message.
		///
		/// Benchmark complexity considerations: O(index + weight_limit).
		#[pallet::call_index(1)]
		#[pallet::weight(
			T::WeightInfo::execute_overweight_page_updated().max(
			T::WeightInfo::execute_overweight_page_removed()).saturating_add(*weight_limit)
		)]
		pub fn execute_overweight(
			origin: OriginFor<T>,
			message_origin: MessageOriginOf<T>,
			page: PageIndex,
			index: T::Size,
			weight_limit: Weight,
		) -> DispatchResultWithPostInfo {
			ensure_signed(origin)?;
			let actual_weight =
				Self::do_execute_overweight(message_origin, page, index, weight_limit)?;
			Ok(Some(actual_weight).into())
		}
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L341-443)
```rust
		/// Process a message delivered by the MessageQueue pallet.
		/// IMPORTANT!! This method does not roll back storage changes on error.
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
