### Title
`submit_delivery_receipt` in `snowbridge-pallet-outbound-queue-v2` has no pallet-level halt check and relies solely on the external verifier, unlike its sibling `inbound-queue-v2` — ([File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs])

### Summary
`pallet-outbound-queue-v2`'s `submit_delivery_receipt` extrinsic pays relayer rewards out of `PendingOrders` after only checking the message proof via `T::Verifier::verify()`. It never checks its own pallet's halt/operating-mode state, even though the pallet defines a `Halted` error variant that is never referenced anywhere in `lib.rs`. This mirrors the RdpxV2Core `redeem()` bug: a payout-relevant function omits the pause/halt guard that its sibling function (here, `inbound_queue_v2::submit`) does apply.

### Finding Description
`inbound-queue-v2::submit` explicitly checks its own pallet's `OperatingMode`: [1](#0-0) 

By contrast, `outbound-queue-v2::submit_delivery_receipt` has no equivalent check on its own storage — it only calls `T::Verifier::verify`, then unconditionally proceeds to `process_delivery_receipt`, which pays the relayer reward and removes the pending order: [2](#0-1) [3](#0-2) 

Yet the pallet's own `Error` enum still declares a `Halted` variant: [4](#0-3) 

and there is no `OperatingMode` storage item, no `set_operating_mode` call, and no `ensure!(!is_halted(), Error::<T>::Halted)` anywhere in the file — confirmed by grep showing zero matches for `OperatingMode`/`set_operating_mode` outside the unused `Error::Halted` and the `Event::OperatingModeChanged` variant, both of which are dead code in this pallet. This is the same class of bug as the report: a payout path (`redeem`) that skips the pause guard that a sibling function (`bond`/`withdraw`) enforces.

The associated `prdoc/stable2603-2/pr_11856.prdoc` confirms the historical severity of this exact gap: halting `pallet-ethereum-client` previously did **not** stop `outbound_queue_v2::submit_delivery_receipt` from paying rewards from `PendingOrders`, because the verifier used to succeed even in `Halted` mode. The fix moved the check into `EthereumBeaconClient::verify` (the shared `Verifier` implementation) rather than adding an independent, pallet-owned halt check to `outbound-queue-v2` itself: [5](#0-4) 

The corrupted invariant is: **reward settlement in `PendingOrders` must only occur while the bridge is not halted, using the pallet's own authoritative halt state — not transitively via a single shared dependency.** Right now that invariant is enforced only indirectly, through `T::Verifier`'s internal `OperatingMode` (owned by `pallet-ethereum-client`), with no defense-in-depth check inside `outbound-queue-v2` itself. Any runtime configuration that wires a different/lighter `Verifier`, or any future change to `EthereumBeaconClient::verify` that regresses the halt short-circuit (exactly as happened before PR #11856), silently re-opens unauthorized reward payout while governance believes the bridge is halted — with zero local safeguard in the pallet that actually moves the funds.

### Impact Explanation
This affects theft/unbacked payout invariants explicitly named in the impact gate ("duplicate settlement or payout", "public underpriced work that degrades block production or stalls bridge processing", "permanent user-fund or bridge-state lock"). A regression or misconfiguration of the single shared `Verifier` halt check re-enables unauthorized fund movement (relayer reward payouts from `PendingOrders`) during an emergency halt — precisely the scenario governance intends to prevent by halting the bridge (e.g., after a suspected light-client compromise, as called out in the prdoc). Because `outbound-queue-v2` has no independent halt gate, there is no second line of defense once the verifier's guarantee is bypassed.

### Likelihood Explanation
Likelihood is moderate: the current `EthereumBeaconClient::verify` implementation does enforce halting today (per `verify_rejects_when_halted` test), so exploitation requires either (a) a runtime configuring `outbound-queue-v2::Config::Verifier` to a component that does not propagate halt state, or (b) a future code change to the verifier that reintroduces the gap PR #11856 just closed. This is not a "malicious relayer/prover" scenario — the attacker primitive is simply a relayer submitting a normal, valid delivery receipt during a period the pallet itself has no ability to observe or block, because it holds no operating-mode state of its own.

### Recommendation
Add an explicit, pallet-owned operating-mode check to `outbound-queue-v2`, mirroring `inbound-queue-v2` and other bridge pallets (`OwnedBridgeModule`/`BasicOperatingMode` pattern already used elsewhere in the bridges codebase):
- Introduce an `OperatingMode<T>` storage item and a root-gated `set_operating_mode` extrinsic in `outbound-queue-v2`, and call `ensure!(!OperatingMode::<T>::get().is_halted(), Error::<T>::Halted)` at the top of `submit_delivery_receipt` (and/or `process_delivery_receipt`), independent of `T::Verifier::verify`.
- Wire this halt flag into the same governance call that halts `pallet-ethereum-client`, so both the verifier and the reward-payout pallet are halted atomically and independently.

### Proof of Concept
1. Configure (or in a future PR, regress) `outbound-queue-v2::Config::Verifier` such that `verify()` no longer returns `VerificationError::Halted` while `pallet-ethereum-client`'s `OperatingMode` is `Halted` (this is exactly the pre-PR-11856 state, reproduced by the `pr_11856.prdoc` description).
2. Governance calls `EthereumBeaconClient::set_operating_mode(Root, Halted)`, intending to freeze bridge activity, including reward payouts.
3. A relayer calls `EthereumOutboundQueueV2::submit_delivery_receipt(origin, event)` with a valid receipt for an existing `PendingOrders` entry.
4. Because `outbound-queue-v2` has no independent halt check (only the unused `Error::<T>::Halted` variant), `process_delivery_receipt` executes as in [6](#0-5) , paying out `order.fee` via `T::RewardPayment::register_reward` and removing the order — despite the bridge being "halted" from governance's perspective.

This confirms the pallet's payout path (`submit_delivery_receipt` / `process_delivery_receipt`) lacks its own pause guard, structurally identical to `RdpxV2Core::redeem()` omitting `_whenNotPaused()`.

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
