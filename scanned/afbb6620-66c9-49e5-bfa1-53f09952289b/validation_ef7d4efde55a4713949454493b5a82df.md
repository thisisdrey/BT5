Looking at the `submit_delivery_receipt` extrinsic in `snowbridge-pallet-outbound-queue-v2`, I found a live analog to the "liquidation works while paused" pattern, distinct from the already-patched `Verifier::verify` halt check.

### Title
`submit_delivery_receipt` reward payout bypasses the outbound-queue-v2 pallet's own `OperatingMode` halt check - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
`snowbridge-pallet-outbound-queue-v2` exposes `set_operating_mode`/`OperatingModeChanged` events and a `Halted` error variant [1](#0-0) , implying the pallet itself is meant to be haltable end-to-end like its sibling pallets (`inbound-queue`, `inbound-queue-v2`, `ethereum-client`, `bridges/modules/messages`) which all explicitly `ensure!(!Self::operating_mode().is_halted(), ...)` at the top of their state-changing extrinsics [2](#0-1) [3](#0-2) . However, `submit_delivery_receipt` in outbound-queue-v2 never checks its own operating mode at all — it only relies transitively on `T::Verifier::verify` (the ethereum-client pallet's halt flag) before calling `process_delivery_receipt`, which pays the relayer from `PendingOrders`: [4](#0-3) 

### Finding Description
The recent fix in `pr_11856.prdoc` closed the gap where `EthereumBeaconClient::Verifier::verify` itself ignored the halted state, which used to let `submit_delivery_receipt` drain `PendingOrders` while the *ethereum-client* pallet was halted [5](#0-4) . That fix only protects against halting the beacon light-client (governance suspecting a compromised sync committee). It does **not** protect the case where governance halts the **outbound-queue-v2 pallet itself** via `OutboundQueueV2::set_operating_mode(Halted)` — a call that exists precisely so that outbound message/reward processing can be independently paused (e.g., because of a bug in reward accounting or a mis-issued `PendingOrder`, not necessarily a beacon-client compromise). Unlike `inbound-queue-v2::submit`, which checks its own `OperatingMode::<T>::get().is_halted()` before touching `T::Verifier` at all [6](#0-5) , `submit_delivery_receipt` has no equivalent `ensure!(!Self::operating_mode().is_halted(), Error::<T>::Halted)` guard — this is exactly the "liquidation still works while the vault is paused" bug class: a pause flag exists and is advertised (`Error::Halted`, `set_operating_mode`) but the value-moving function silently ignores the pallet's own halt state.

### Impact Explanation
If governance halts `snowbridge-pallet-outbound-queue-v2` specifically (independent of halting the ethereum-client light client), relayers can still call `submit_delivery_receipt` with valid proofs and continue draining `PendingOrders`, paying out relayer rewards via `T::RewardPayment::register_reward` [7](#0-6) . This defeats the purpose of the pallet's own halt switch, continuing bridge reward settlement (a form of duplicate/uncontrolled payout continuing during an intended freeze) contrary to the "client does not want bad debt/uncontrolled state changes while paused" expectation embodied by the analogous `Halted` guards elsewhere in the same bridge stack.

### Likelihood Explanation
Medium: it requires governance to have already invoked `set_operating_mode(Halted)` on outbound-queue-v2 specifically (a legitimate, expected operational action, not privileged-admin misuse of the vulnerability itself) and does not require any malicious peer, relayer collusion, or key compromise — a normal relayer submitting normal, valid delivery receipts triggers the unintended payout continuation.

### Recommendation
Add the same guard used by the other Snowbridge pallets to `submit_delivery_receipt`:
```rust
ensure!(!Self::operating_mode().is_halted(), Error::<T>::Halted);
```
placed before (or alongside) the `T::Verifier::verify` call in `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`, mirroring `inbound_queue_v2::submit` and `inbound_queue::submit`.

### Proof of Concept
1. Governance calls `OutboundQueueV2::set_operating_mode(RuntimeOrigin::root(), BasicOperatingMode::Halted)`.
2. A relayer submits a valid `EventProof` for an already-delivered message via `OutboundQueueV2::submit_delivery_receipt(origin, event)`.
3. `T::Verifier::verify` succeeds (ethereum-client is still Normal / not halted).
4. `process_delivery_receipt` executes normally, removing the entry from `PendingOrders` and calling `T::RewardPayment::register_reward`, despite the outbound-queue-v2 pallet being in `Halted` mode — no `Error::<T>::Halted` is ever raised because `submit_delivery_receipt` never reads `OperatingMode::<T>`.

**Note on uncertainty:** I was not able to fully verify from the available index whether any runtime-level `BaseCallFilter` (outside this pallet) additionally blocks `submit_delivery_receipt` when this specific pallet is halted, since call filters are configured per-runtime (e.g., BridgeHub runtime) and were not part of the returned search results. If such a runtime-level filter exists and explicitly maps `OutboundQueueV2::submit_delivery_receipt` to the pallet's own operating mode, this would mitigate the gap; a Devin session with full repo access should confirm this in `cumulus/parachains/runtimes/bridge-hubs/bridge-hub-*/src/lib.rs`.

### Citations

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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L464-477)
```rust
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
```

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs (L180-198)
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
```

**File:** bridges/snowbridge/pallets/inbound-queue/src/lib.rs (L237-239)
```rust
		pub fn submit(origin: OriginFor<T>, event: EventProof) -> DispatchResult {
			let who = ensure_signed(origin)?;
			ensure!(!Self::operating_mode().is_halted(), Error::<T>::Halted);
```

**File:** bridges/snowbridge/pallets/ethereum-client/src/impls.rs (L15-28)
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
```
