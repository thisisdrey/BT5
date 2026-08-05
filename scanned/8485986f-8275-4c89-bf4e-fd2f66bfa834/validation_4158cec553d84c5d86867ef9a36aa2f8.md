## Analog Found: Dead `Halted` Error and Missing `OperatingMode` Circuit Breaker in `snowbridge-pallet-outbound-queue-v2`

### Title
Missing pallet-level `OperatingMode`/halt enforcement in `pallet_outbound_queue_v2` leaves declared `Error::Halted` unreachable and removes governance's ability to stop delivery-receipt processing - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
The external report's core defect is a state variable (`isPaused`) that exists in name only: it is declared, referenced conceptually in the contract's intent, but never wired to any setter or any guard, so the "pause" protection is dead code. The same pattern exists in `pallet_outbound_queue_v2`: the pallet declares `Error::<T>::Halted` and imports `BasicOperatingMode` for its `OperatingModeChanged` event [1](#0-0) , but it never defines an `OperatingMode` storage item, never exposes a `set_operating_mode` extrinsic, and never checks any halt flag before executing `submit_delivery_receipt` or `do_process_message`. This is unlike the sibling pallets `snowbridge-pallet-outbound-queue` (v1) and `snowbridge-pallet-inbound-queue-v2`, which both implement and enforce this exact circuit breaker.

### Finding Description
`pallet_outbound_queue_v2::Error` contains a `Halted` variant with the doc comment "The pallet is halted" [2](#0-1) , and the `Event` enum contains `OperatingModeChanged { mode: BasicOperatingMode }` [3](#0-2) . These declarations strongly imply the pallet is intended to support a governance/root-controlled halt, exactly as its sibling pallets do.

Compare with `pallet_outbound_queue` (v1), which fully implements the mechanism: [4](#0-3) 
and `pallet_inbound_queue_v2`, which enforces the check on every call: [5](#0-4) 

In `pallet_outbound_queue_v2`, however, there is:
- No `OperatingMode` (or similarly named) `#[pallet::storage]` item anywhere in the pallet.
- No `set_operating_mode` call in the `#[pallet::call]` block, which only exposes `submit_delivery_receipt` [6](#0-5) .
- No `ensure!(!<halt>, Error::<T>::Halted)` guard in `submit_delivery_receipt`, `process_delivery_receipt`, or `do_process_message` [7](#0-6) .

The only halt-related behavior that does exist comes indirectly from `T::Verifier::verify` returning `VerificationError::Halted` if the underlying light-client verifier itself is halted [8](#0-7) , which is confirmed by the test `poc_m1`/`submit_delivery_receipt_succeeds_after_unhalt` that toggles a mock verifier's halted flag, not any pallet storage [9](#0-8) . This means the pallet has no independent, governance-controlled circuit breaker of its own — its only "halt" path is entirely delegated to (and dependent on) the verifier's own halted state.

### Impact Explanation
`do_process_message` mutates `Messages`, `MessageLeaves`, `Nonce`, and creates a `PendingOrder` fee entry for every message queued for Ethereum delivery [10](#0-9) , and `process_delivery_receipt` pays out relayer rewards from `PendingOrders` and removes the entry [11](#0-10) . If an incident requires stopping this pallet independently (e.g., a bug is found in `DeliveryReceipt::try_from`, the gas metering logic, or reward accounting, while the underlying beacon/verifier is still healthy), there is no on-chain mechanism to halt it short of a runtime upgrade — unlike every comparable bridge pallet in this repository (GRANDPA, parachains, relayers, inbound-queue-v2, outbound-queue v1) which all expose `set_operating_mode`/`OwnedBridgeModule`. This directly matches the "Public underpriced work that degrades block production or stalls bridge processing" and "permanent... bridge-state lock" impact classes, because reward payouts and message commitments continue unconditionally regardless of any pallet-level pause intent.

### Likelihood Explanation
This is not an attacker-triggered exploit but a structural gap that is certain to manifest whenever governance needs to stop v2 outbound processing independently of the verifier: the code paths (`submit_delivery_receipt`, `do_process_message`) are always reachable by any signed account/relayer, and there is no code path, storage item, or extrinsic that could ever gate them on a pallet-level halt. The declared `Error::Halted` variant is unreachable dead code confirming the gap was intended to be filled but wasn't, exactly mirroring the `isPaused` bug class in the external report.

### Recommendation
Add an `OperatingMode` storage item and a root/governance-gated `set_operating_mode` extrinsic to `pallet_outbound_queue_v2`, mirroring `pallet_outbound_queue` v1's implementation (`OperatingMode::<T>::put(mode)` plus event emission) [4](#0-3) . Then guard `submit_delivery_receipt` and `do_process_message` with `ensure!(!OperatingMode::<T>::get().is_halted(), Error::<T>::Halted)`, consistent with `pallet_inbound_queue_v2::submit` [12](#0-11) .

### Proof of Concept
1. Deploy a runtime with `pallet_outbound_queue_v2` configured as in `bridges/snowbridge/pallets/outbound-queue-v2/src/mock.rs`.
2. Attempt to locate any extrinsic named `set_operating_mode` on `OutboundQueueV2` — none exists; the pallet's `#[pallet::call]` block only contains `submit_delivery_receipt` [6](#0-5) .
3. Even with root origin, there is no way to stop `do_process_message`/`submit_delivery_receipt` from continuing to enqueue messages, generate `PendingOrder` fee liabilities, and pay relayer rewards, other than halting the underlying `T::Verifier` (a separate component/pallet) — demonstrating the pallet's own advertised `Halted` error and `OperatingModeChanged` event are unreachable/never emitted anywhere in `lib.rs`.

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L219-243)
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

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L265-279)
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
	}
```

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs (L180-211)
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
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/test.rs (L390-449)
```rust
// Reward processing must be blocked while the bridge is halted: `submit_delivery_receipt`
// should not pay out `PendingOrder` fees if the verifier reports the bridge as halted.
#[test]
fn poc_m1() {
	new_tester().execute_with(|| {
		let nonce = 1;
		let fee: u128 = 1_000_000;
		let order = PendingOrder { nonce, fee, block_number: System::block_number() };
		PendingOrders::<Test>::insert(nonce, order);

		let relayer: AccountId32 = [7u8; 32].into();
		let origin = RuntimeOrigin::signed(relayer);
		let event = Box::new(mock_event_proof());

		set_verifier_halted(true);

		assert_noop!(
			OutboundQueue::submit_delivery_receipt(origin.clone(), event.clone()),
			Error::<Test>::Verification(VerificationError::Halted)
		);

		let order_after = PendingOrders::<Test>::get(nonce).expect("order still present");
		assert_eq!(order_after.fee, fee);

		set_verifier_halted(false);
	});
}

// After governance resumes the bridge, legitimate delivery receipts flow through again:
// the order is paid out and removed from storage.
#[test]
fn submit_delivery_receipt_succeeds_after_unhalt() {
	new_tester().execute_with(|| {
		let nonce = 0;
		let fee: u128 = 1_000_000;
		let order = PendingOrder { nonce, fee, block_number: System::block_number() };
		PendingOrders::<Test>::insert(nonce, order);

		let relayer: AccountId32 = [7u8; 32].into();
		let origin = RuntimeOrigin::signed(relayer);
		let event = Box::new(mock_valid_event_proof());

		// Bridge halted — receipt rejected, order untouched.
		set_verifier_halted(true);
		assert_noop!(
			OutboundQueue::submit_delivery_receipt(origin.clone(), event.clone()),
			Error::<Test>::Verification(VerificationError::Halted)
		);
		assert!(PendingOrders::<Test>::get(nonce).is_some());

		// Bridge resumed — same receipt succeeds and the order is settled.
		set_verifier_halted(false);
		assert_ok!(OutboundQueue::submit_delivery_receipt(origin, event));
		assert!(PendingOrders::<Test>::get(nonce).is_none());

		System::assert_has_event(mock::RuntimeEvent::OutboundQueue(Event::MessageDelivered {
			nonce,
		}));
	});
}
```
