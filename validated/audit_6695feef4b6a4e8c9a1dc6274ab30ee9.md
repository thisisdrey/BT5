Confirmed: `snowbridge-pallet-outbound-queue-v2` defines a `Halted` error variant and an `OperatingModeChanged` event referencing `BasicOperatingMode`, but there is no `OperatingMode` storage item, no `set_operating_mode` extrinsic in `#[pallet::call]`, and no halt check anywhere in `SendMessage::validate`/`deliver` [1](#0-0)  or in `do_process_message`/`process_delivery_receipt` [2](#0-1) . This is the direct on-chain analog of the `Pausable`-without-`whenNotPaused` pattern.

### Title
Outbound queue v2 defines a `Halted`/`OperatingMode` mechanism but never enforces or exposes it - ([File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs])

### Summary
`snowbridge-pallet-outbound-queue-v2` (the v2 Ethereum-bound message committer used by BridgeHub) declares `Error::Halted` and `Event::OperatingModeChanged { mode: BasicOperatingMode }`, mirroring the halt/pause mechanism that its sibling pallet `outbound-queue` (v1) actually implements [3](#0-2) . In v1, `OperatingMode` storage exists, `set_operating_mode` is a callable extrinsic, and `deliver()` checks `Self::operating_mode().is_halted()` before enqueuing [4](#0-3) . In v2, none of that exists: there is no `OperatingMode` storage item, no `set_operating_mode` call, and `SendMessage::deliver` and `ProcessMessage::process_message` unconditionally enqueue/process messages with no halt gate at all [5](#0-4) .

### Finding Description
The v2 pallet's `Config`, error, and event definitions strongly imply a halt/governance-pause capability is supported ("The pallet is halted" error, `OperatingModeChanged` event) [6](#0-5) , but:
- No `OperatingMode`/`PalletOperatingMode` storage value is declared anywhere in the pallet.
- The `#[pallet::call]` block only exposes `submit_delivery_receipt` — there is no `set_operating_mode` extrinsic to actually flip the mode [5](#0-4) .
- `SendMessage::validate`/`deliver` in `send_message_impl.rs` only checks message size, never a halted state, before calling `T::MessageQueue::enqueue_message` [7](#0-6) .
- `do_process_message` in `lib.rs`, invoked via `ProcessMessage::process_message`, processes and commits messages (assigning nonces, creating `PendingOrder`s, appending to the Merkle leaf set) with zero halt/operating-mode gate [8](#0-7) .

This is exactly the `Pausable`-inherited-but-not-wired pattern from the external report: the "pause" primitives (error variant, event) exist to suggest an operator/governance safety valve, but the actual storage flag and the enforcement checks on the public dispatch paths (`deliver`, `process_message`) are absent. Anyone (any sibling parachain, or Ethereum-side governance channel via `snowbridge-pallet-system-v2::send`) can continue to enqueue and have Ethereum-bound commands committed even in a scenario where operators believe (based on the v1 pattern and the pallet's own error/event surface) that they can halt the channel.

### Impact Explanation
If BridgeHub governance or an incident responder needs to halt the v2 Ethereum-outbound channel during an upgrade, an Ethereum-side incident, gas-meter compromise, or gateway contract issue, there is no on-chain lever to do so for this specific pallet — unlike v1, which supports `set_operating_mode(Halted)` to stop new messages from being enqueued while allowing already-queued ones to drain safely. Messages will keep being committed into the Merkle root and dispatched toward Ethereum regardless of any attempt to pause, which can extend chain/bridge exposure during an incident and stall any remediation that depends on stopping new outbound message flow.

### Likelihood Explanation
High: this is a structural code-path gap, not a race condition or a rare edge case. Every call to `deliver()` or every message delivered by the `MessageQueue` to `process_message` will always succeed regardless of intended operating mode, because the enforcement code simply doesn't exist. No attacker action is required to trigger it — normal operation already bypasses any pause a governance actor may believe is available, and there's no way to introduce one without a runtime upgrade.

### Recommendation
Add an `OperatingMode`/`PalletOperatingMode` storage item plus a `set_operating_mode` extrinsic (mirroring `outbound-queue` v1 and `BasicOperatingMode`), and gate `SendMessage::deliver` and `do_process_message` (or `ProcessMessage::process_message`) behind an `ensure!(!Self::operating_mode().is_halted(), Error::<T>::Halted)` check, consistent with how v1 handles this (including allowing the governance channel to bypass halt if that's the intended design, as v1 does for `PRIMARY_GOVERNANCE_CHANNEL`).

### Proof of Concept
1. Deploy/observe `snowbridge-pallet-outbound-queue-v2` on a BridgeHub-style runtime.
2. Attempt to call `set_operating_mode` on this pallet — no such extrinsic exists in its `#[pallet::call]` implementation, confirmed against `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs` lines 293–318 (only `submit_delivery_receipt` is exposed).
3. Regardless of any intended incident response, any sibling parachain (via XCM `ExportMessage`) or `snowbridge-pallet-system-v2::send` can call `SendMessage::validate`/`deliver`, which unconditionally enqueues the message (`send_message_impl.rs` lines 23–43) with no halted check.
4. The enqueued message is delivered to `process_message` → `do_process_message`, which unconditionally converts, stores, and commits it into `MessageLeaves`/`Messages` and creates a `PendingOrder` (lib.rs lines 341–443), again with no halt check — proving the channel cannot be stopped through any on-chain pause mechanism that the pallet's own error/event types suggest should exist.

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/send_message_impl.rs (L17-44)
```rust
impl<T> SendMessage for Pallet<T>
where
	T: Config,
{
	type Ticket = Message;

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
}
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L225-243)
```rust
	#[pallet::error]
	pub enum Error<T> {
		/// The message is too large
		MessageTooLarge,
		/// The pallet is halted
		Halted,
		/// Invalid Channel
		InvalidChannel,
		/// Invalid Envelope
		InvalidEnvelope,
		/// Message verification error
		Verification(VerificationError),
		/// Invalid Gateway
		InvalidGateway,
		/// Pending nonce does not exist
		InvalidPendingNonce,
		/// Reward payment failed
		RewardPaymentFailed,
	}
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L293-318)
```rust
	#[pallet::call]
	impl<T: Config> Pallet<T>
	where
		<T as frame_system::Config>::AccountId: From<[u8; 32]>,
	{
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
	}
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L341-480)
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

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L205-213)
```rust
	#[pallet::error]
	pub enum Error<T> {
		/// The message is too large
		MessageTooLarge,
		/// The pallet is halted
		Halted,
		/// Invalid Channel
		InvalidChannel,
	}
```

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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/process_message_impl.rs (L11-28)
```rust
impl<T: Config> ProcessMessage for Pallet<T> {
	type Origin = T::AggregateMessageOrigin;
	fn process_message(
		message: &[u8],
		origin: Self::Origin,
		meter: &mut WeightMeter,
		_: &mut [u8; 32],
	) -> Result<bool, ProcessMessageError> {
		let weight = T::WeightInfo::do_process_message();
		if meter.try_consume(weight).is_err() {
			Self::deposit_event(Event::MessagePostponed {
				payload: message.to_vec(),
				reason: ProcessMessageError::Overweight(weight),
			});
			return Err(ProcessMessageError::Overweight(weight));
		}
		Self::do_process_message(origin, message)
	}
```
