Found the analog. Confirmed: `outbound-queue-v2::submit_delivery_receipt` has **no** `OperatingMode`/`Halted` check of its own — unlike `inbound-queue-v2::submit`, which explicitly does `ensure!(!OperatingMode::<T>::get().is_halted(), Error::<T>::Halted);` before calling the verifier. [1](#0-0) [2](#0-1) 

### Title
`submit_delivery_receipt` in `pallet-outbound-queue-v2` lacks its own operating-mode/halt guard, allowing relayer reward payout to continue during an emergency halt of the pallet - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
`snowbridge-pallet-outbound-queue-v2` defines a `Halted` error variant and even emits `OperatingModeChanged` events, exactly mirroring `pallet-inbound-queue-v2`'s halt mechanism. But the actual dispatchable, `submit_delivery_receipt`, never reads any `OperatingMode` storage or performs the `is_halted()` check that `inbound-queue-v2::submit` performs. This is the same class of bug as the Notional report: an "emergency stop" primitive (there: emergency withdraw not reaching the nested/underlying pool; here: halting the outbound-queue-v2 pallet not actually stopping its own dispatch path) that is defined but does not cover the actual state-mutating/value-releasing entrypoint.

### Finding Description
`inbound-queue-v2` explicitly gates `submit` on its own `OperatingMode` storage before invoking the verifier: [3](#0-2) 

`outbound-queue-v2`'s `submit_delivery_receipt` calls `T::Verifier::verify` directly and then unconditionally proceeds to `process_delivery_receipt`, which pays out relayer rewards from `PendingOrders` — with no `ensure!(!OperatingMode::…)` check anywhere in this pallet: [2](#0-1) [4](#0-3) 

The `Error::<T>::Halted` variant exists in the pallet's error enum but is dead code — it is declared but never actually enforced by any call in this file: [5](#0-4) 

This is precisely the pattern fixed for the *v1/verifier* path in a related change (`prdoc/stable2603-2/pr_11856.prdoc`), which pushed the halt into `pallet-ethereum-client`'s `Verifier::verify` specifically because `outbound_queue_v2::submit_delivery_receipt` could keep paying relayer rewards from `PendingOrders` while governance thought the bridge was halted: [6](#0-5) 

That prdoc shows the *verifier* is now halted-aware, so proof verification itself will fail with `VerificationError::Halted` once `pallet-ethereum-client` is halted. However, `pallet-outbound-queue-v2`'s own halt story is a separate, independently-configured `OperatingMode` (with its own storage and `set_operating_mode`-equivalent semantics used by `inbound-queue-v2`/`inbound-queue`), and this pallet's error type advertises the same guarantee (`Halted` variant) without wiring it to any actual check on `submit_delivery_receipt`. Governance/relayer operators relying on the presence of a per-pallet `OperatingMode`/`Halted` semantics for `outbound-queue-v2` — consistent with every sibling pallet (`inbound-queue`, `inbound-queue-v2`) that exposes exactly this pattern — get none of that protection for this specific extrinsic, unless the shared underlying `pallet-ethereum-client` verifier is independently halted first. If the two halt states can be (or are intended to be) toggled independently — e.g. an operator halts `outbound-queue-v2` specifically without halting the shared ethereum-client verifier used by other pallets — the reward payout path stays fully live.

### Impact Explanation
`process_delivery_receipt` unconditionally reads `PendingOrders` and calls `T::RewardPayment::register_reward`, releasing the fee amount to the relayer/reward account and removing the pending order: [7](#0-6) 
If an emergency requires stopping bridge state transitions (e.g. suspected relayer/verifier compromise, gateway/nonce inconsistency, or any other reason to halt), the absence of a self-contained halt check on this pallet's only call means reward settlement can continue in exactly the scenario the halt mechanism is supposed to prevent — i.e. permanent mis-settlement of reward state / unbacked payout while the system is believed to be stopped. This matches the "duplicate settlement or payout" / "public underpriced work that degrades block production or stalls bridge processing" impact class for Snowbridge BridgeHub code.

### Likelihood Explanation
No privileged action or malicious relayer collusion is required beyond the ordinary, expected relayer flow of calling `submit_delivery_receipt` with a valid Ethereum-side receipt/proof — this is the pallet's normal, unprivileged, permissionless public entrypoint. The gap only manifests when governance halts `outbound-queue-v2` (or expects halted semantics for it) while the shared verifier remains live; the likelihood of this operational divergence is uncertain without confirmation of runtime wiring (i.e., whether the concrete runtime configures a shared or a per-pallet `OperatingMode`/verifier instance), which I could not fully verify from the index — this is a real gap in the pallet's own code regardless of runtime configuration, but its exploitability depends on how the runtime composes `Config::Verifier` across pallets.

### Recommendation
Add an explicit `OperatingMode` storage item (or reuse an equivalent halt-check hook) to `pallet-outbound-queue-v2`, and gate `submit_delivery_receipt` with `ensure!(!OperatingMode::<T>::get().is_halted(), Error::<T>::Halted)` before verification and before `process_delivery_receipt`, mirroring `inbound-queue-v2::submit`. This makes the pallet's own declared `Halted` error variant actually enforced, independent of whatever halt state the underlying `Verifier` implementation exposes.

### Proof of Concept
1. Deploy/configure a runtime with `pallet-outbound-queue-v2` where `Config::Verifier` is *not* simultaneously halted (e.g. a different verifier instance/config than the one governance intends to stop, or before governance has propagated the halt to the shared ethereum-client).
2. Governance calls whatever mechanism is expected to halt `outbound-queue-v2` operations (per the pallet's documented `Halted` error and the pattern used by `inbound-queue-v2`/`inbound-queue`).
3. A relayer submits a valid `EventProof` via `submit_delivery_receipt` for an existing entry in `PendingOrders`.
4. Because `submit_delivery_receipt` performs no `OperatingMode`/halt check of its own ( [2](#0-1) ), verification succeeds (verifier not halted) and `process_delivery_receipt` pays the relayer reward and removes the order — despite the pallet believing/declaring itself halted.

### Citations

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

**File:** prdoc/stable2603-2/pr_11856.prdoc (L1-17)
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
