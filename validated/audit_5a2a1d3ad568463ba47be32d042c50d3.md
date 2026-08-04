### Title
Public `process_delivery_receipt()` / `process_message()` bypass proof verification when called directly, allowing spoofed relayer-reward registration - ([File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs])

### Summary
`Pallet::<T>::process_delivery_receipt()` in `snowbridge-pallet-outbound-queue-v2` and `Pallet::<T>::process_message()` in `snowbridge-pallet-inbound-queue-v2` are declared as `pub fn` (not `pub(crate)`) on the pallet's inherent `impl` block, exactly like the `LiquidationDistributor.distribute()` bug: the reward/accounting logic itself performs no proof verification, and instead relies entirely on the caller (the `submit_delivery_receipt`/`submit` extrinsics) to have already run `T::Verifier::verify(...)` before invoking it.

### Finding Description
In `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`, the intended flow is: [1](#0-0) 
`submit_delivery_receipt` verifies the Ethereum event proof via `T::Verifier::verify`, decodes it into a `DeliveryReceipt`, then calls `Self::process_delivery_receipt(relayer, receipt)`.

However, `process_delivery_receipt` is a standalone `pub fn` that performs the actual state-changing accounting (paying out the relayer reward and removing the `PendingOrders` entry) with only a check that `receipt.gateway` matches `T::GatewayAddress`: [2](#0-1) 

The `gateway` field, `nonce`, and `reward_address` all come from the caller-supplied `DeliveryReceipt` struct — not from any value that was independently re-derived from the verified proof inside this function. The function trusts that whoever calls it already ran `T::Verifier::verify`. Because the function is `pub` rather than `pub(crate)`, it is exposed as part of the pallet's public Rust API to every other pallet/crate compiled into the runtime, exactly the same class of bug described in the Gondi report: an accounting-critical function ("distribute the reward") is public, and the caller-provided authenticity of its inputs (the equivalent of `loan.principalAddress`) is not re-validated inside the function that performs the payout.

The same structural flaw exists in `snowbridge-pallet-inbound-queue-v2`: [3](#0-2) 
`submit` verifies the proof, then calls `Self::process_message(who, message)`, which itself independently re-checks `message.gateway` and `Nonce` — but registers the reward (`T::RewardPayment::register_reward`) based purely on the fields of the caller-supplied `Message` struct, with no re-verification of the underlying Ethereum proof inside `process_message` itself.

### Impact Explanation
If any other pallet, XCM handler, or future code path in the runtime were wired to call `Pallet::<T>::process_delivery_receipt` or `Pallet::<T>::process_message` directly (bypassing the `submit`/`submit_delivery_receipt` extrinsics and their `T::Verifier::verify` calls), an attacker could register arbitrary relayer rewards for an unverified/fabricated `nonce`, `gateway`, and `reward_address`/`fee`, resulting in unbacked mint/reward payout from the bridge's reward pot to an attacker-chosen beneficiary — precisely the "theft or unbacked mint or unlock" and "duplicate settlement or payout" impact classes called out in the Polkadot SDK Impact Gate.

### Likelihood Explanation
Currently, in the shipped runtime wiring, both `process_delivery_receipt` and `process_message` are only invoked from their respective verified extrinsics (`submit_delivery_receipt`, `submit`), so no live unauthenticated external call path is confirmed to exist today. The vulnerability is a latent access-control/API-design defect — identical in kind to the audited Gondi issue — rather than a demonstrated end-to-end exploit in the current runtime. Any future refactor, benchmarking helper, or additional pallet composing with these queues that calls these `pub fn`s directly (as the pallet's own test suite already does, e.g. `EthereumInboundQueueV2::process_message(relayer_account.clone(), message.clone())` in the emulated tests) would reintroduce the exact Gondi-style vulnerability, since nothing in the function signature or body prevents it.

### Recommendation
Downgrade `process_delivery_receipt` and `process_message` to `pub(crate) fn`, or otherwise enforce that they can only be reached through the verified extrinsics, following the same mitigation Gondi applied: restrict the caller of the accounting/reward-payout function to the verified entry point, rather than trusting caller-supplied proof-derived fields inside a broadly `pub` function.

### Proof of Concept
Conceptual PoC (illustrates the broken invariant, not a live exploit in current wiring):
```rust
// Any code in the runtime crate with access to the pallet type can call this directly,
// skipping T::Verifier::verify entirely:
snowbridge_pallet_outbound_queue_v2::Pallet::<Runtime>::process_delivery_receipt(
    attacker_account,
    DeliveryReceipt {
        gateway: GatewayAddress::get(), // attacker knows the constant
        nonce: some_pending_nonce,      // any nonce with fee > 0 in PendingOrders
        reward_address: attacker_bytes, // arbitrary beneficiary
    },
)?;
// -> T::RewardPayment::register_reward(&attacker_account, ..., order.fee) is executed
//    with no Ethereum proof ever verified.
```
This mirrors the Gondi PoC where `distribute()` was called directly with a forged `principalAddress`, causing the Pool to mis-account funds it never validated.

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

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs (L180-245)
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

	impl<T: Config> Pallet<T> {
		pub fn process_message(relayer: T::AccountId, message: Message) -> DispatchResult {
			// Verify that the message was submitted from the known Gateway contract
			ensure!(T::GatewayAddress::get() == message.gateway, Error::<T>::InvalidGateway);

			let (nonce, relayer_fee) = (message.nonce, message.relayer_fee);

			// Verify the message has not been processed
			ensure!(!Nonce::<T>::get(nonce), Error::<T>::InvalidNonce);

			// Mark message as received
			Nonce::<T>::set(nonce);

			let message_id = T::MessageProcessor::process_message(relayer.clone(), message)
				.map_err(|e| match e {
					MessageProcessorError::ProcessMessage(e) => e,
					MessageProcessorError::ConvertMessage(e) => Error::<T>::from(e).into(),
					MessageProcessorError::SendMessage(e) => Error::<T>::from(e).into(),
				})?;

			// Pay relayer reward
			let tip = Tips::<T>::take(nonce).unwrap_or_default();
			let total_tip = relayer_fee.saturating_add(tip);
			if total_tip > 0 {
				T::RewardPayment::register_reward(&relayer, T::DefaultRewardKind::get(), total_tip);
			}

			// Emit event with the message_id
			Self::deposit_event(Event::MessageReceived { nonce, message_id });

			Ok(())
		}
```
