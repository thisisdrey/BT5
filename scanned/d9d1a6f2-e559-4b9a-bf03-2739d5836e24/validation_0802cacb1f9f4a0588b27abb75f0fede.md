### Title
`snowbridge-pallet-outbound-queue-v2` has no working halt/pause enforcement — `Error::Halted` is dead code - ([File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs])

### Summary
The external report's core defect is: a pause/halt gate exists conceptually (error code, event) but the actual enforcement check targets the wrong/absent state, so the operation proceeds even when the system is supposed to be halted. The same class of defect exists in `snowbridge-pallet-outbound-queue-v2`: the pallet defines `Error::<T>::Halted` and `Event::OperatingModeChanged`, mirroring the halt machinery used by its sibling pallets (`outbound-queue` v1, `inbound-queue-v2`, `ethereum-client`), but it never wires up an `OperatingMode` storage item, a `set_operating_mode` extrinsic, or an `is_halted()` guard anywhere in its dispatch/enqueue paths.

### Finding Description
Every other Snowbridge pallet that can move value or dispatch messages implements the halt pattern consistently:
- `inbound-queue-v2::submit` checks `ensure!(!OperatingMode::<T>::get().is_halted(), Error::<T>::Halted);` [1](#0-0) 
- `outbound-queue` (v1) `deliver()` checks `ensure!(!Self::operating_mode().is_halted(), SendError::Halted);` for all non-governance channels [2](#0-1) 
- `ethereum-client::submit` and its `Verifier::verify` also check `is_halted()` [3](#0-2) 

`outbound-queue-v2`, however, declares the same `Halted` error variant and `OperatingModeChanged` event [4](#0-3)  but never defines an `OperatingMode` storage value, never exposes a `set_operating_mode` call, and never invokes `is_halted()` anywhere in the crate (confirmed by grep — only the 3 dead references in `lib.rs` for the error/event definitions exist; `send_message_impl.rs` has zero halt-related matches).

Concretely:
- `SendMessage::deliver` for v2 enqueues every ticket unconditionally with no halt check at all: [5](#0-4) 
- `submit_delivery_receipt` (the extrinsic that pays relayer rewards from `PendingOrders`) performs no local operating-mode check either — it only calls `T::Verifier::verify`, which enforces the shared Ethereum light-client halt flag, not any outbound-queue-v2-specific governance halt: [6](#0-5) 
- `process_delivery_receipt` unconditionally pays out `order.fee` to the reward account once a receipt decodes and the nonce is pending: [7](#0-6) 

The design intent (per the `Halted`/`OperatingModeChanged` types) is clearly to allow governance to halt this pallet independently, exactly like its v1 counterpart and `inbound-queue-v2`. Because the enforcement was never wired in, there is no way to pause message enqueueing or delivery-receipt reward payout for `outbound-queue-v2` specifically. The only lever governance has is halting the shared Ethereum light client (`ethereum-client`), which is a blunt, chain-wide instrument that also blocks legitimate inbound traffic and beacon header updates — not a substitute for a pallet-scoped halt.

### Impact Explanation
If governance needs to halt the v2 outbound message pipeline in response to a suspected bug in `do_process_message`, `commit`, or the reward-accounting logic in `process_delivery_receipt`/`AddTip::add_tip`, there is no functioning mechanism to do so short of halting the entire beacon light client. Any relayer can continue to submit `submit_delivery_receipt` and drain `PendingOrders` fees, and any sibling parachain / `system-v2` pallet can continue enqueuing outbound messages, during an incident that governance believes it has "paused." This matches the report's core primitive: an operation that should be blocked by an active pause mechanism instead proceeds, risking fund payout and continued processing during an emergency.

### Likelihood Explanation
No privileged action, malicious relayer, or admin abuse is required to trigger the *underlying* gap — the bug is structural and present unconditionally: the check simply does not exist. It manifests the moment governance attempts to halt `outbound-queue-v2` specifically (a normal incident-response action) and discovers reward payout/message enqueueing is unaffected. Given `outbound-queue-v2` and `inbound-queue-v2` are the newer bridge pipeline actively developed alongside `system-v2`/`system-frontend`, this is a live gap in the current codebase, not a historical, already-patched issue (unlike the `pr_11856.prdoc` fix for `ethereum-client`, which addressed a related but distinct halt gap).

### Recommendation
- **Short term:** Add an `OperatingMode` storage item and `set_operating_mode` call to `outbound-queue-v2`, mirroring `outbound-queue` v1 and `inbound-queue-v2`. Add `ensure!(!OperatingMode::<T>::get().is_halted(), Error::<T>::Halted);` (or `SendError::Halted` for `deliver`) to both `SendMessage::deliver` in `send_message_impl.rs` and to `submit_delivery_receipt` in `lib.rs`, consistent with how governance-channel messages should still bypass the halt (as v1 does for `PRIMARY_GOVERNANCE_CHANNEL`).
- **Long term:** Add integration tests analogous to `outbound-queue`'s `submit_upgrade_message_success_when_queue_halted` test that assert `outbound-queue-v2` rejects non-governance sends/receipts while halted and that governance messages still pass, closing the coverage gap that let this scaffolding go unwired.

### Proof of Concept
1. Deploy a runtime with `snowbridge-pallet-outbound-queue-v2` configured normally, with a pending order already recorded in `PendingOrders` (created via `do_process_message`).
2. Assume governance has decided to halt bridge outbound processing (there is no `set_operating_mode` extrinsic to actually call, which is itself evidence of the gap — attempt to locate one in the pallet's `Call` enum and observe it does not exist).
3. As an ordinary relayer, call `submit_delivery_receipt(origin, event)` with a valid Ethereum delivery-receipt proof for the pending nonce.
4. Observe that `submit_delivery_receipt` succeeds and pays the relayer via `T::RewardPayment::register_reward` in `process_delivery_receipt`, because no `is_halted()` guard exists in this call path — contrast with `bridges/snowbridge/pallets/outbound-queue-v2/src/test.rs` `poc_m1`, which shows the pallet only becomes "halted" through the *verifier's* halt flag (`set_verifier_halted(true)`), not through any pallet-native operating mode.

### Citations

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs (L185-188)
```rust
		pub fn submit(origin: OriginFor<T>, event: Box<EventProof>) -> DispatchResult {
			let who = ensure_signed(origin)?;
			ensure!(!OperatingMode::<T>::get().is_halted(), Error::<T>::Halted);

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

**File:** bridges/snowbridge/pallets/ethereum-client/src/impls.rs (L21-31)
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
			.map_err(|e| InvalidExecutionProof(e.into()))?;
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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L446-480)
```rust
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
