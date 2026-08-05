Audit Report

## Title
Outbound queue v2 keeps accepting, committing, and fee-obligating new messages after the bridge is halted - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

## Summary
`pallet-ethereum-client::Verifier::verify` now refuses to run while the light client is `Halted` [1](#0-0) , which stops `outbound_queue_v2::submit_delivery_receipt` from draining `PendingOrders` during a halt since it calls `T::Verifier::verify` before paying out [2](#0-1) . However, the message-acceptance path — `SendMessage::validate`/`deliver` [3](#0-2)  and `do_process_message` [4](#0-3)  — has no halt gate at all, unlike `inbound-queue-v2::submit`, which explicitly checks `ensure!(!OperatingMode::<T>::get().is_halted(), Error::<T>::Halted)`.

## Finding Description
`do_process_message` unconditionally decodes the queued message, appends it to `Messages`/`MessageLeaves` (later merkle-committed into the header digest via `commit()` and relayed to Ethereum), and inserts a new `PendingOrder{nonce, fee, block_number}` into `PendingOrders` while advancing `Nonce` [5](#0-4)  — with no check of any halt/operating-mode flag anywhere in that function. The pallet declares an `Error::Halted` variant and an `Event::OperatingModeChanged` event [6](#0-5) , but there is no `OperatingMode` storage item and no `set_operating_mode` call anywhere in the pallet — confirmed by inspecting the full `lib.rs`, which only contains `Messages`, `MessageLeaves`, `Nonce`, and `PendingOrders` storage items [7](#0-6) . The only halt awareness in the whole pallet is inside `submit_delivery_receipt`, indirectly via `T::Verifier::verify`, which internally calls `Self::operating_mode().is_halted()` in `pallet-ethereum-client` [1](#0-0) . This is a real gap in the "emergency stop" design: halting the light client blocks reward payout via the reused `Verifier::verify` check, but does not stop new outbound messages from being accepted, merkle-committed into the parachain header digest, and queued with new `PendingOrder` fee obligations.

## Impact Explanation
This matches the "public underpriced work that degrades... or stalls bridge processing" impact band: an emergency-stop meant to freeze bridge state transitions during a suspected light-client compromise fails to stop the message-acceptance half of the bridge's write path. Outbound message commitments (irreversible merkle roots written into the parachain header digest) and fee-bearing `PendingOrders` continue to accumulate during exactly the window when the light client is considered untrusted, which undermines the security rationale for halting — new outbound state and payout liabilities are the exact writes governance intends to freeze.

## Likelihood Explanation
High likelihood: it triggers under the exact governance scenario the fix in `pr_11856.prdoc` was designed for (suspected beacon light-client compromise). No privileged or malicious actor is required — any ordinary user or parachain sending XCM to Ethereum via `EthereumBlobExporter::deliver` or `snowbridge-pallet-system-v2::send` continues to work and accrue new `PendingOrders` fee obligations during the halt window, since neither `validate`, `deliver`, nor `do_process_message` consult any halt state.

## Recommendation
Add an explicit `OperatingMode` storage item and `set_operating_mode` call to `outbound-queue-v2` (mirroring `inbound-queue-v2`), and gate `do_process_message` (and ideally `SendMessage::validate`) on it, in addition to the existing `Verifier`-based check used in `submit_delivery_receipt`. Alternatively, have `do_process_message` also consult `T::Verifier`'s/`pallet-ethereum-client`'s halted state before accepting and committing new outbound messages, so message acceptance and reward payout freeze together during an emergency stop.

## Proof of Concept
1. Governance calls `EthereumBeaconClient::set_operating_mode(Halted)` after suspecting a sync-committee compromise.
2. A user on AssetHub sends an XCM message destined for Ethereum; it is exported and enqueued via `outbound-queue-v2::SendMessage::deliver` [8](#0-7)  — no halt check occurs.
3. `T::MessageQueue` invokes `ProcessMessage::process_message` → `Pallet::do_process_message`, which appends the message to `Messages`/`MessageLeaves`, inserts a new `PendingOrder{nonce, fee}`, and advances `Nonce` [5](#0-4)  — none of this consults the light client's halted flag.
4. `on_finalize` calls `Self::commit()`, computing a merkle root and writing it into the parachain header digest via `deposit_log` [9](#0-8)  — new outbound state is committed on-chain despite the halt.
5. Once governance resumes the light client, relayers submit `submit_delivery_receipt` for those messages and drain the `PendingOrders` that accumulated during the halt window, none of which were ever prevented from forming.

### Citations

**File:** bridges/snowbridge/pallets/ethereum-client/src/impls.rs (L21-30)
```rust
	fn verify(event_log: &Log, proof: &Proof) -> Result<(), VerificationError> {
		// Refuse to verify any Ethereum-side proof while the beacon light client is halted.
		// Governance halts the light client when it suspects a compromise (e.g. sync committee
		// takeover), at which point any signed headers/receipts must be treated as untrusted.
		// Covers every Verifier consumer, including `inbound_queue_v2::submit` and
		// `outbound_queue_v2::submit_delivery_receipt` (which would otherwise still drain
		// pending relayer rewards while the bridge is halted).
		ensure!(!Self::operating_mode().is_halted(), VerificationError::Halted);

		Self::verify_execution_proof(&proof.execution_proof)
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L219-230)
```rust
		/// Set OperatingMode
		OperatingModeChanged { mode: BasicOperatingMode },
		/// Delivery Proof received
		MessageDelivered { nonce: u64 },
	}

	#[pallet::error]
	pub enum Error<T> {
		/// The message is too large
		MessageTooLarge,
		/// The pallet is halted
		Halted,
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L245-271)
```rust
	/// Messages to be committed in the current block. This storage value is killed in
	/// `on_initialize`, so will not end up bloating state.
	///
	/// Is never read in the runtime, only by offchain message relayers.
	/// Because of this, it will never go into the PoV of a block.
	///
	/// Inspired by the `frame_system::Pallet::Events` storage value
	#[pallet::storage]
	#[pallet::unbounded]
	pub type Messages<T: Config> = StorageValue<_, Vec<OutboundMessage>, ValueQuery>;

	/// Hashes of the ABI-encoded messages in the [`Messages`] storage value. Used to generate a
	/// merkle root during `on_finalize`. This storage value is killed in `on_initialize`, so state
	/// at each block contains only root hash of messages processed in that block. This also means
	/// it doesn't have to be included in PoV.
	#[pallet::storage]
	#[pallet::unbounded]
	pub type MessageLeaves<T: Config> = StorageValue<_, Vec<H256>, ValueQuery>;

	/// The current nonce for the messages
	#[pallet::storage]
	pub type Nonce<T: Config> = StorageValue<_, u64, ValueQuery>;

	/// Pending orders to relay
	#[pallet::storage]
	pub type PendingOrders<T: Config> =
		StorageMap<_, Twox64Concat, u64, PendingOrder<BlockNumberFor<T>>, OptionQuery>;
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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L322-339)
```rust
		pub(crate) fn commit() {
			let count = MessageLeaves::<T>::decode_len().unwrap_or_default() as u64;
			if count == 0 {
				return;
			}

			// Create merkle root of messages
			let root = merkle_root::<<T as Config>::Hashing, _>(MessageLeaves::<T>::stream_iter());

			let digest_item: DigestItem = SnowbridgeDigestItem::SnowbridgeV2(root).into();

			// Insert merkle root into the header digest
			<frame_system::Pallet<T>>::deposit_log(digest_item);

			T::OnNewCommitment::on_new_commitment(root);

			Self::deposit_event(Event::MessagesCommitted { root, count });
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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/send_message_impl.rs (L23-43)
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

	fn deliver(ticket: Self::Ticket) -> Result<H256, SendError> {
		let origin = ticket.origin.into();

		let message =
			BoundedVec::try_from(ticket.encode()).map_err(|_| SendError::MessageTooLarge)?;

		T::MessageQueue::enqueue_message(message.as_bounded_slice(), origin);
		Self::deposit_event(Event::MessageQueued { message: ticket.clone() });
		Ok(ticket.id)
	}
```
