This confirms it: `pallet-outbound-queue-v2` (Snowbridge) defines an `Error::<T>::Halted` variant and even documents halt semantics via `BasicOperatingMode`/`OperatingModeChanged` imports, but the pallet has **no `OperatingMode` storage item, no `set_operating_mode` extrinsic, and no halt-check at all** — unlike its sibling pallets. `submit_delivery_receipt` (the only callable extrinsic besides internal message processing) never enforces any shutdown/pause gate before paying relayer rewards. [1](#0-0) [2](#0-1) 

Compare with `pallet-inbound-queue-v2`, which does have its own `OperatingMode` storage and checks it explicitly before verification: [3](#0-2) 

### Title
Snowbridge `outbound-queue-v2` defines a `Halted` error but has no operating-mode gate to enforce it on `submit_delivery_receipt` - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
`pallet-outbound-queue-v2` mirrors the shutdown-flag pattern used across other Snowbridge/bridge pallets (`BasicOperatingMode`, `Error::Halted`, `Event::OperatingModeChanged`), but unlike `inbound-queue`, `inbound-queue-v2`, `outbound-queue` (v1), and `ethereum-client`, it never actually defines an `OperatingMode` storage item or `set_operating_mode` call, and `submit_delivery_receipt` never checks any halt condition of its own before dispatching a reward payout. This is the direct structural analog of the reported `Omnipool.desactivate()` bug: a "shutdown" concept exists in the code (error variant, docstring intent) but is not wired into the state-changing entrypoint that it is supposed to gate.

### Finding Description
Every other bridge-facing pallet in this repository enforces `ensure!(!OperatingMode::is_halted(), Error::Halted)` at the top of its state-changing extrinsic:
- `pallet-inbound-queue`: `ensure!(!Self::operating_mode().is_halted(), Error::<T>::Halted);` before `Verifier::verify` [4](#0-3) 
- `pallet-inbound-queue-v2`: same pattern [5](#0-4) 
- `pallet-ethereum-client`: `ensure!(!Self::operating_mode().is_halted(), Error::<T>::Halted);` in `submit` [6](#0-5) 

`pallet-outbound-queue-v2` declares the same `Halted` error variant and imports `BasicOperatingMode` and even emits `OperatingModeChanged` in its `Event` enum, signalling the intent to support the same halt semantics — but no `OperatingMode` storage value, no `set_operating_mode` call, and no `ensure!(...is_halted...)` check exist anywhere in the crate (confirmed via `send_message_impl.rs`, `process_message_impl.rs`, and `lib.rs`). `submit_delivery_receipt` goes straight from `ensure_signed` to `T::Verifier::verify`, and on success pays out the relayer reward from `PendingOrders` via `process_delivery_receipt`. [7](#0-6) 

The only remaining protection is the *ethereum-client's own* halt check inside `Verifier::verify`, added in a separate fix (PR #11856) specifically to close this exact "pay from `PendingOrders` while halted" gap for the *light client's* halted state: [8](#0-7) . That fix, however, only covers the case where `pallet-ethereum-client` itself is halted. It does **not** give `outbound-queue-v2` its own independent shutdown mechanism the way `inbound-queue-v2` has. Any governance action, incident-response procedure, or runtime configuration that expects to pause outbound reward settlement specifically (without touching the shared Ethereum light client used by unrelated pallets like `system-frontend` XCM export) has no lever to pull — the "shutdown" primitive for this pallet class simply is not implemented where the codebase's own conventions say it should be.

### Impact Explanation
If governance (or an automated incident response) halts `outbound-queue-v2` under the reasonable assumption that the exposed `Halted` error and `OperatingModeChanged` event indicate a working pause mechanism (as it does for every sibling pallet), no such halt actually exists to invoke, and even if the pallet were extended with `set_operating_mode` in isolation without updating `submit_delivery_receipt`, the halt would be silently ineffective — `PendingOrders` fees would keep draining to relayers exactly as in the reported `Omnipool.desactivate()` bug, where setting `isShutdown = true` had no effect on `depositFor()`. This breaks the "public underpriced work / duplicate settlement / permanent lock" class of impact only if relayer reward settlement needs to be haltable independently during an incident; absent that independent halt, the impact is limited to the missing safety control itself rather than an active fund-theft path today, since the shared ethereum-client halt still blocks verification.

### Likelihood Explanation
Low-to-moderate: this is not exploitable by an unprivileged attacker today because the ethereum-client's halt (PR #11856) already blocks `Verifier::verify` for real Ethereum-side proofs, so reward payout can't proceed while the light client is halted. The gap is an incident-response/operational-safety gap (missing independent circuit breaker for `outbound-queue-v2`) rather than a currently-triggerable unauthorized-fund-drain path, so it does not meet the "unprivileged attacker, no admin/governance needed" bar as cleanly as the original Omnipool report. I flag this with reduced confidence given that determination of "intended design vs. missing safety control" ultimately requires the maintainers' intent, which I could not fully verify beyond the code and prdoc evidence collected.

### Recommendation
Add an `OperatingMode` storage item and `set_operating_mode` extrinsic to `pallet-outbound-queue-v2`, consistent with `pallet-inbound-queue-v2`, and enforce `ensure!(!Self::operating_mode().is_halted(), Error::<T>::Halted);` at the start of `submit_delivery_receipt`, so that halting outbound reward settlement does not depend entirely on the shared ethereum-client's operating mode.

### Proof of Concept
Not directly exploitable as unprivileged-attacker fund theft in the current state (the ethereum-client halt check from PR #11856 still blocks verification). The reproducible artifact is a code-review PoC:
1. Observe `Error::<T>::Halted` declared in `pallet-outbound-queue-v2` [9](#0-8)  and `Event::OperatingModeChanged` [10](#0-9) .
2. Search the crate for any `OperatingMode` storage value or `set_operating_mode` call — none exists (confirmed by grep across `bridges/snowbridge/pallets/outbound-queue-v2/**`, only 3 matches, all in `lib.rs`, all just the error/event/import lines).
3. Confirm `submit_delivery_receipt` calls `T::Verifier::verify` directly with no local halt check [7](#0-6) , then unconditionally proceeds to `process_delivery_receipt`, which pays `T::RewardPayment::register_reward` from `PendingOrders` [11](#0-10) .

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

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs (L183-198)
```rust
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

**File:** bridges/snowbridge/pallets/inbound-queue/src/lib.rs (L237-243)
```rust
		pub fn submit(origin: OriginFor<T>, event: EventProof) -> DispatchResult {
			let who = ensure_signed(origin)?;
			ensure!(!Self::operating_mode().is_halted(), Error::<T>::Halted);

			// submit message to verifier for verification
			T::Verifier::verify(&event.event_log, &event.proof)
				.map_err(|e| Error::<T>::Verification(e))?;
```

**File:** bridges/snowbridge/pallets/ethereum-client/src/lib.rs (L220-224)
```rust
		pub fn submit(origin: OriginFor<T>, update: Box<Update>) -> DispatchResultWithPostInfo {
			ensure_signed(origin)?;
			ensure!(!Self::operating_mode().is_halted(), Error::<T>::Halted);
			Self::process_update(&update)
		}
```

**File:** bridges/snowbridge/pallets/ethereum-client/src/impls.rs (L21-29)
```rust
	fn verify(event_log: &Log, proof: &Proof) -> Result<(), VerificationError> {
		// Refuse to verify any Ethereum-side proof while the beacon light client is halted.
		// Governance halts the light client when it suspects a compromise (e.g. sync committee
		// takeover), at which point any signed headers/receipts must be treated as untrusted.
		// Covers every Verifier consumer, including `inbound_queue_v2::submit` and
		// `outbound_queue_v2::submit_delivery_receipt` (which would otherwise still drain
		// pending relayer rewards while the bridge is halted).
		ensure!(!Self::operating_mode().is_halted(), VerificationError::Halted);

```
