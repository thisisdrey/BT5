Confirmed: `outbound-queue-v2` has no `OperatingMode` storage, no `set_operating_mode` extrinsic, and no halt check anywhere in `validate`, `deliver`, or `do_process_message` — the only place any halt state is consulted is inside `submit_delivery_receipt` via `T::Verifier::verify` (which checks `pallet-ethereum-client`'s halted flag, per the already-applied fix in `prdoc/stable2603-2/pr_11856.prdoc`). The pallet even declares `Error::Halted` and `Event::OperatingModeChanged` variants that are dead code — never emitted, never checked.

### Title
Outbound queue v2 keeps committing messages and accruing `PendingOrders` fee obligations after the bridge is halted - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
`pallet-ethereum-client`'s `Verifier::verify` now refuses to run while the light client is `Halted` (fix in PR #11856), which stops `outbound_queue_v2::submit_delivery_receipt` from draining `PendingOrders` during a halt. However, that fix only protects the *reward-claim* path. The *message-acceptance* path of the same pallet — `SendMessage::validate`/`deliver` (`send_message_impl.rs`) and `ProcessMessage::process_message` → `do_process_message` (`process_message_impl.rs`, `lib.rs:343-443`) — has no halt gate at all, unlike `inbound-queue-v2::submit`, which explicitly checks `ensure!(!OperatingMode::<T>::get().is_halted(), Error::<T>::Halted)` before doing any work [1](#0-0) .

### Finding Description
`do_process_message` unconditionally decodes the queued XCM-originated message, appends it to `Messages`/`MessageLeaves` (which get committed into the header digest and relayed to Ethereum), and — critically — inserts a new `PendingOrder{nonce, fee, block_number}` into `PendingOrders` and advances `Nonce`, all with no check of any halt/operating-mode flag [2](#0-1) . The pallet declares an `Error::Halted` variant and an `Event::OperatingModeChanged` event (clearly intended plumbing for a governance halt, mirroring `inbound-queue-v2`) but there is no `OperatingMode` storage item and no `set_operating_mode` call anywhere in `outbound-queue-v2` [3](#0-2) . The only halt awareness in the whole pallet is inside `submit_delivery_receipt`, via `T::Verifier::verify`, which checks `pallet-ethereum-client`'s *own* halted state [4](#0-3) , exactly as documented in the delivery-receipt fix [5](#0-4) .

This reproduces the bug-class from the external report: an emergency-halt mechanism (the pausable dependency) is meant to freeze a coupled process end-to-end, but only blocks one side of it. In the ERC721Pool case, pausing the NFT blocked repay/liquidate while interest still accrued; here, halting the Ethereum light client (the intended "emergency stop" for the whole bridge, invoked "after a suspected beacon light client compromise" per the prdoc) blocks reward payout, but does **not** stop new outbound messages from being accepted, committed into the header digest, and queued with fresh `PendingOrder` fee obligations. Governance believes the bridge is frozen; in reality BridgeHub keeps minting new outbound commitments and new fee liabilities the whole time the halt is in effect, growing unresolved `PendingOrders` state that becomes payable in a lump sum the instant the halt is lifted (or that a compromised/careless relayer can later claim via legitimate delivery receipts once the halt is removed, having accumulated during the very window meant to be frozen).

### Impact Explanation
This falls squarely in the "public underpriced work that degrades... or stalls bridge processing" / "duplicate settlement or payout" impact band: an emergency-stop meant to fully suspend bridge state transitions during a suspected compromise fails to stop half of the bridge's write path. Messages keep being merkle-committed into the parachain header (irreversible, provable-to-Ethereum state) and fee-bearing `PendingOrders` keep accumulating during exactly the window when the light client is untrusted, undermining the security rationale for halting in the first place (an attacker exploiting a compromised sync committee/beacon light client is precisely the scenario governance halts for, and outbound message commitments continue unabated).

### Likelihood Explanation
High likelihood of triggering under the exact governance action documented as the reason for halting (suspected beacon light-client compromise). It requires no privileged/malicious actor to trigger — any normal user/parachain sending XCM to Ethereum via `EthereumBlobExporter::deliver` or `snowbridge-pallet-system-v2::send` continues to work and accrue fee obligations during the halt window, since neither `validate`, `deliver`, nor `do_process_message` consult any halt flag.

### Recommendation
Add an explicit `OperatingMode` storage item and `set_operating_mode` call to `outbound-queue-v2` (mirroring `inbound-queue-v2`), and gate `do_process_message` (and ideally `SendMessage::validate`) on it, in addition to (not instead of) the existing `Verifier`-based halt check for `submit_delivery_receipt`. Alternatively, have `do_process_message` also consult `T::Verifier`'s/`pallet-ethereum-client`'s halted state before accepting/committing new outbound messages, so both message acceptance and reward payout freeze together during an emergency stop.

### Proof of Concept
1. Governance calls `EthereumBeaconClient::set_operating_mode(Halted)` after suspecting a sync-committee compromise (per the scenario in `prdoc/stable2603-2/pr_11856.prdoc`).
2. A user on AssetHub sends an XCM message destined for Ethereum; it is exported and enqueued via `outbound-queue-v2::SendMessage::deliver` (`send_message_impl.rs:34-43`) — no halt check.
3. `T::MessageQueue` invokes `ProcessMessage::process_message` → `Pallet::do_process_message` (`lib.rs:343-443`), which appends the message to `Messages`/`MessageLeaves`, inserts a new `PendingOrder{nonce, fee}`, and advances `Nonce` — none of this consults `pallet-ethereum-client`'s halted flag.
4. `on_finalize` calls `Self::commit()`, computing a merkle root and writing it into the parachain header digest, and firing `on_new_commitment` — new outbound state is now committed on-chain and observable to Ethereum-side relayers, despite the "emergency stop."
5. Once governance later resumes the light client, relayers can submit `submit_delivery_receipt` for those messages and drain the newly built-up `PendingOrders`, none of which were ever prevented from forming during the halt.

### Citations

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs (L185-198)
```rust
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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L307-317)
```rust
			let relayer = ensure_signed(origin)?;

			// submit message to verifier for verification
			T::Verifier::verify(&event.event_log, &event.proof)
				.map_err(|e| Error::<T>::Verification(e))?;

			let receipt = DeliveryReceipt::try_from(&event.event_log)
				.map_err(|_| Error::<T>::InvalidEnvelope)?;

			Self::process_delivery_receipt(relayer, receipt)
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

**File:** prdoc/stable2603-2/pr_11856.prdoc (L1-18)
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

```
