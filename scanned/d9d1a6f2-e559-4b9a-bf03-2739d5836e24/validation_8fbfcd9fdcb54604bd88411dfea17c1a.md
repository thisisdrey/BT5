## Title
`snowbridge-pallet-outbound-queue-v2` has no independent halt gate — relayer reward payouts continue even though the pallet declares (but never wires up) `Error::Halted` / `Event::OperatingModeChanged` - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
This is a structural analog of the C4 M-02 pattern: a single kill-switch is supposed to gate a class of privileged/economic operations, but the wiring between the switch and one of the consumers is incomplete, so that consumer keeps running unaffected while the rest of the system believes it is halted. In GnosisTrade/Broker the flaw was "too broad" (one auction's failure disabled an unrelated auction type). In `snowbridge-pallet-outbound-queue-v2` the flaw is "too narrow": the pallet defines the vocabulary for an operating-mode halt (`Error::<T>::Halted`, `Event::OperatingModeChanged`) but never implements the `set_operating_mode` extrinsic or an `OperatingMode` storage item, and `submit_delivery_receipt` never checks any halted flag of its own — its only gate against a compromised bridge is `T::Verifier::verify`, i.e. `pallet-ethereum-client`'s halted state.

### Finding Description
`submit_delivery_receipt` in [1](#0-0)  only calls `T::Verifier::verify(...)` before processing the delivery receipt and paying out the relayer reward via `Self::process_delivery_receipt`. There is no local `ensure!(!OperatingMode::<T>::get().is_halted(), ...)` check inside this pallet, even though the pallet declares:

- `Error::<T>::Halted` at [2](#0-1) 
- `Event::<T>::OperatingModeChanged` at [3](#0-2) 

Neither is ever used or emitted anywhere in the file — there is no `OperatingMode` storage item and no `set_operating_mode` call in the `#[pallet::call]` block (which contains only `submit_delivery_receipt`, call_index 1) at [4](#0-3) . This is unlike its sibling pallets `snowbridge-pallet-inbound-queue` and `snowbridge-pallet-inbound-queue-v2`, both of which implement a real `set_operating_mode` call and check `Self::operating_mode().is_halted()` / `OperatingMode::<T>::get().is_halted()` at the top of `submit`, e.g. [5](#0-4)  and [6](#0-5) .

Consequently, `outbound-queue-v2`'s only defense against reward payout during an emergency is transitive: it depends entirely on `pallet-ethereum-client`'s own `OperatingMode` halted flag being checked inside `Verifier::verify`, as fixed in [7](#0-6) . The prdoc for that fix explicitly documents that this transitive dependency was the entire safety net for this exact reward-drain scenario: [8](#0-7) . There is no defense-in-depth: if the pallet is ever configured with a `Verifier` implementation that does not enforce the halted invariant (e.g. a different/mocked/future light-client integration, or if `pallet-ethereum-client`'s halt check is ever regressed or bypassed by a code path other than `verify`), `outbound-queue-v2` has zero independent circuit breaker of its own, despite advertising one in its `Error`/`Event` enums. This mirrors the root cause of the referenced bug class: a security control (halt) that one component (`BackingManager`/`RevenueTrader` in the Reserve case, `outbound-queue-v2` here) implicitly relies on another component to enforce, rather than enforcing it locally and atomically at the point of fund movement.

### Impact Explanation
If the sole enforcement point (`pallet-ethereum-client::verify`) is ever bypassed, misconfigured, or the runtime substitutes a different `Verifier` type for `outbound-queue-v2::Config::Verifier` that doesn't itself halt-check, relayers can continue to submit delivery receipts and drain `PendingOrders` reward funds via `T::RewardPayment::register_reward` at [9](#0-8)  while governance believes the bridge is fully halted — an unbacked/duplicate-style payout against the intent of the halt, i.e. exactly the class of impact ("theft or unbacked mint or unlock... duplicate settlement or payout") called out in the impact gate. Because the pallet never checks its own halt flag, there is no independent stop for this fund-movement path; it is entirely dependent on a different pallet's internal check remaining correct forever.

### Likelihood Explanation
Low-to-moderate: today this is masked because `Verifier::verify` in `pallet-ethereum-client` does correctly halt-check (already patched via PR referenced in `pr_11856.prdoc`). But the missing local check is a latent single point of failure — any future change to the `Verifier` wiring, a runtime that plugs in an alternate verifier, or a regression in `ethereum-client::verify` reintroduces the exact drain scenario the prdoc describes, with no second line of defense in `outbound-queue-v2` itself. The unused `Error::Halted` / `Event::OperatingModeChanged` in the pallet strongly suggest the local gate was intended but never actually implemented, unlike its `inbound-queue` and `inbound-queue-v2` siblings.

### Recommendation
Add a genuine `OperatingMode` storage item and `set_operating_mode` extrinsic to `snowbridge-pallet-outbound-queue-v2`, mirroring `inbound-queue-v2`, and gate `submit_delivery_receipt` with `ensure!(!OperatingMode::<T>::get().is_halted(), Error::<T>::Halted)` as an independent, pallet-local check in addition to (not instead of) the `Verifier::verify` halt check. This ensures reward-payout settlement is halted atomically and locally whenever governance halts this pallet, without relying solely on another pallet's internal invariant.

### Proof of Concept
Code-level trace (no live-network requirement, purely from repository evidence):
1. `outbound-queue-v2::Error::Halted` and `Event::OperatingModeChanged` are declared but never referenced elsewhere in the crate — confirmed by inspecting the full file: [2](#0-1) .
2. The `#[pallet::call]` block only contains `submit_delivery_receipt` (call_index 1); no `set_operating_mode` call exists: [4](#0-3) .
3. `submit_delivery_receipt` gates only on `T::Verifier::verify`, then unconditionally calls `process_delivery_receipt`, which pays the relayer reward and removes the `PendingOrder`: [10](#0-9) .
4. Compare with `inbound-queue-v2::submit`, which independently checks `OperatingMode::<T>::get().is_halted()` before calling the verifier: [11](#0-10) .
5. The project's own prdoc for `pr_11856` confirms that, prior to hardening `Verifier::verify`, `outbound_queue_v2::submit_delivery_receipt` "could continue to process receipts and pay out relayer rewards from `PendingOrders` while governance had halted the bridge" — i.e., this exact payout path had no independent halt of its own and depended entirely on the verifier: [12](#0-11) .

This confirms `outbound-queue-v2` still has no local circuit breaker; its safety today is a single point of trust in `pallet-ethereum-client::verify`, not defense-in-depth, which is the same structural weakness (a fund-movement path implicitly, not explicitly, gated by an unrelated component's halt state) as the external report's Broker/GnosisTrade/DutchTrade coupling issue.

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L219-220)
```rust
		/// Set OperatingMode
		OperatingModeChanged { mode: BasicOperatingMode },
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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L293-480)
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

	impl<T: Config> Pallet<T> {
		/// Generate a messages commitment and insert it into the header digest
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

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs (L180-212)
```rust
	#[pallet::call]
	impl<T: Config> Pallet<T> {
		/// Submit an inbound message originating from the Gateway contract on Ethereum
		#[pallet::call_index(0)]
		#[pallet::weight(T::WeightInfo::submit())]
		pub fn submit(origin: OriginFor<T>, event: Box<EventProof>) -> DispatchResult {
			let who = ensure_signed(origin)?;
			ensure!(!OperatingMode::<T>::get().is_halted(), Error::<T>::Halted);

			// submit message for verification
			T::Verifier::verify(&event.event_log, &event.proof)
				.map_err(|e| Error::<T>::Verification(e))?;

			// Decode event log into a bridge message
			let message =
				Message::try_from(&event.event_log).map_err(|_| Error::<T>::InvalidMessage)?;

			Self::process_message(who, message)
		}

		/// Halt or resume all pallet operations. May only be called by root.
		#[pallet::call_index(1)]
		#[pallet::weight((T::DbWeight::get().reads_writes(1, 1), DispatchClass::Operational))]
		pub fn set_operating_mode(
			origin: OriginFor<T>,
			mode: BasicOperatingMode,
		) -> DispatchResult {
			ensure_root(origin)?;
			OperatingMode::<T>::set(mode);
			Self::deposit_event(Event::OperatingModeChanged { mode });
			Ok(())
		}
	}
```

**File:** bridges/snowbridge/pallets/inbound-queue/src/lib.rs (L232-325)
```rust
	#[pallet::call]
	impl<T: Config> Pallet<T> {
		/// Submit an inbound message originating from the Gateway contract on Ethereum
		#[pallet::call_index(0)]
		#[pallet::weight(T::WeightInfo::submit())]
		pub fn submit(origin: OriginFor<T>, event: EventProof) -> DispatchResult {
			let who = ensure_signed(origin)?;
			ensure!(!Self::operating_mode().is_halted(), Error::<T>::Halted);

			// submit message to verifier for verification
			T::Verifier::verify(&event.event_log, &event.proof)
				.map_err(|e| Error::<T>::Verification(e))?;

			// Decode event log into an Envelope
			let envelope =
				Envelope::try_from(&event.event_log).map_err(|_| Error::<T>::InvalidEnvelope)?;

			// Verify that the message was submitted from the known Gateway contract
			ensure!(T::GatewayAddress::get() == envelope.gateway, Error::<T>::InvalidGateway);

			// Retrieve the registered channel for this message
			let channel =
				T::ChannelLookup::lookup(envelope.channel_id).ok_or(Error::<T>::InvalidChannel)?;

			// Verify message nonce
			<Nonce<T>>::try_mutate(envelope.channel_id, |nonce| -> DispatchResult {
				if *nonce == u64::MAX {
					return Err(Error::<T>::MaxNonceReached.into());
				}
				if envelope.nonce != nonce.saturating_add(1) {
					Err(Error::<T>::InvalidNonce.into())
				} else {
					*nonce = nonce.saturating_add(1);
					Ok(())
				}
			})?;

			// Reward relayer from the sovereign account of the destination parachain, only if funds
			// are available
			let sovereign_account = sibling_sovereign_account::<T>(channel.para_id);
			let delivery_cost = Self::calculate_delivery_cost(event.encode().len() as u32);
			let amount = T::Token::reducible_balance(
				&sovereign_account,
				Preservation::Preserve,
				Fortitude::Polite,
			)
			.min(delivery_cost);
			if !amount.is_zero() {
				T::Token::transfer(&sovereign_account, &who, amount, Preservation::Preserve)?;
			}

			// Decode payload into `VersionedMessage`
			let message = VersionedMessage::decode_all(&mut envelope.payload.as_ref())
				.map_err(|_| Error::<T>::InvalidPayload)?;

			// Decode message into XCM
			let (xcm, fee) = Self::do_convert(envelope.message_id, message.clone())?;

			tracing::info!(
				target: LOG_TARGET,
				?xcm,
				?fee,
				"💫 xcm decoded"
			);

			// Burning fees for teleport
			Self::burn_fees(channel.para_id, fee)?;

			// Attempt to send XCM to a dest parachain
			let message_id = Self::send_xcm(xcm, channel.para_id)?;

			Self::deposit_event(Event::MessageReceived {
				channel_id: envelope.channel_id,
				nonce: envelope.nonce,
				message_id,
				fee_burned: fee,
			});

			Ok(())
		}

		/// Halt or resume all pallet operations. May only be called by root.
		#[pallet::call_index(1)]
		#[pallet::weight((T::DbWeight::get().reads_writes(1, 1), DispatchClass::Operational))]
		pub fn set_operating_mode(
			origin: OriginFor<T>,
			mode: BasicOperatingMode,
		) -> DispatchResult {
			ensure_root(origin)?;
			OperatingMode::<T>::set(mode);
			Self::deposit_event(Event::OperatingModeChanged { mode });
			Ok(())
		}
	}
```

**File:** bridges/snowbridge/pallets/ethereum-client/src/impls.rs (L15-41)
```rust
impl<T: Config> Verifier for Pallet<T> {
	/// Verify a message by verifying the existence of the corresponding
	/// Ethereum log in a block. Returns the log if successful. The execution header containing
	/// the log is sent with the message. The beacon header containing the execution header
	/// is also sent with the message, to check if the header is an ancestor of a finalized
	/// header.
	fn verify(event_log: &Log, proof: &Proof) -> Result<(), VerificationError> {
		// Refuse to verify any Ethereum-side proof while the beacon light client is halted.
		// Governance halts the light client when it suspects a compromise (e.g. sync committee
		// takeover), at which point any signed headers/receipts must be treated as untrusted.
		// Covers every Verifier consumer, including `inbound_queue_v2::submit` and
		// `outbound_queue_v2::submit_delivery_receipt` (which would otherwise still drain
		// pending relayer rewards while the bridge is halted).
		ensure!(!Self::operating_mode().is_halted(), VerificationError::Halted);

		Self::verify_execution_proof(&proof.execution_proof)
			.map_err(|e| InvalidExecutionProof(e.into()))?;

		Self::verify_receipt_inclusion(
			proof.execution_proof.execution_header.receipts_root(),
			event_log.tx_index,
			&proof.receipt_proof,
			event_log,
		)?;

		Ok(())
	}
```

**File:** prdoc/stable2603-2/pr_11856.prdoc (L1-25)
```text
title: 'Snowbridge: halt the Ethereum verifier when the bridge is in emergency stop'

doc:
  - audience: Runtime Dev
    description: |
      When `pallet-ethereum-client` is in `Halted` operating mode, its `Verifier::verify`
      implementation now short-circuits with the new `VerificationError::Halted` instead of
      attempting to verify Ethereum-side proofs.

      Previously, halting the light client only blocked new beacon header updates via
      `EthereumBeaconClient::submit`. Proof verification still ran, which meant
      `inbound_queue_v2::submit` and `outbound_queue_v2::submit_delivery_receipt` could
      continue to process receipts and pay out relayer rewards from `PendingOrders` while
      governance had halted the bridge (e.g. after a suspected beacon light client compromise).

      Halting the verifier closes that gap in one place — covering both inbound dispatch and
      outbound delivery-receipt reward payments.

crates:
  - name: snowbridge-verification-primitives
    bump: major
  - name: snowbridge-pallet-outbound-queue-v2
    bump: major
  - name: snowbridge-pallet-ethereum-client
    bump: patch
```
