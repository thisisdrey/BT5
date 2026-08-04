Found it — `outbound-queue-v2::submit_delivery_receipt` calls `T::Verifier::verify()` but never checks the pallet's own `OperatingMode`/halted flag before verifying and paying out the reward.

### Title
`submit_delivery_receipt` pays relayer rewards without checking the outbound-queue-v2 pallet's own halted operating mode - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
This mirrors the Nibbl "buyout cannot be rejected when paused" pattern: a governance-gated pause is supposed to freeze *all* state-changing effects of a subsystem, but one payout path was left outside the pause check, so the time/nonce-gated settlement (relayer reward payout) still completes while the module is halted.

### Finding Description
`snowbridge-pallet-outbound-queue-v2` exposes `submit_delivery_receipt`, which verifies an Ethereum receipt proof and then calls `process_delivery_receipt` to pay the relayer's fee out of `PendingOrders` and remove the order [1](#0-0) , with the actual fee transfer/removal in `process_delivery_receipt` [2](#0-1) .

Unlike `inbound-queue`/`inbound-queue-v2`, which explicitly gate `submit` on `!Self::operating_mode().is_halted()` before calling the verifier [3](#0-2) [4](#0-3) , `outbound-queue-v2::submit_delivery_receipt` performs no local halted check at all — it only relies on `T::Verifier::verify()` internally checking the *Ethereum beacon client's* halted state [5](#0-4) .

This is precisely the residual gap that a recent fix (`pr_11856`) closed for the beacon-client-halted case — the PR doc explicitly states the previous gap let `outbound_queue_v2::submit_delivery_receipt` "continue to process receipts and pay out relayer rewards from `PendingOrders`" while halted, and fixed it by making the *verifier* itself refuse when the ethereum-client's own operating mode is halted [6](#0-5) , with a regression test `poc_m1` asserting the reward stays blocked when `set_verifier_halted(true)` [7](#0-6) .

However, that fix only covers the case where the **ethereum-client / beacon light client** is halted. It does not add any check for the **outbound-queue-v2 pallet's own `OperatingMode` storage**, which is set independently by governance via `set_operating_mode` and used elsewhere (e.g. `send_message_impl::deliver` gates new message sends on it: `ensure!(!Self::operating_mode().is_halted(), SendError::Halted)` [8](#0-7) ). If governance halts the outbound-queue-v2 pallet itself (e.g. because it suspects a bug in message processing, reward accounting, or fee computation, rather than a beacon-committee compromise), `submit_delivery_receipt` is untouched by that halt: the beacon light client is still `Normal`, so `Verifier::verify` succeeds, and `process_delivery_receipt` will still drain `PendingOrders` and register relayer rewards — exactly like the Nibbl `redeem()`/`withdrawERC721()` functions that lacked the `whenNotPaused` modifier and let a time-gated settlement complete despite the system being paused.

### Impact Explanation
An operator/governance halt of the outbound-queue-v2 pallet is expected to stop *all* pallet-level effects, including irreversible settlement of pending relayer fees. Because `submit_delivery_receipt` is not gated on the pallet's own `OperatingMode`, relayers can continue to claim/settle `PendingOrders` fees during a halt that was specifically meant to freeze this subsystem (e.g. while investigating a fee-calculation or nonce-accounting bug), permanently draining `PendingOrders` state and paying out rewards that governance intended to freeze. This falls under "duplicate settlement or payout" / "public underpriced or unintended work" risk categories for bridge reward payout state, since payout state advances despite an active governance-level stop.

### Likelihood Explanation
Likelihood is moderate: it requires governance to halt `outbound-queue-v2` specifically (not the ethereum-client), which is a legitimate defensive action, after which any unprivileged relayer holding a valid Ethereum receipt proof can still call the public, permissionless `submit_delivery_receipt` extrinsic and be paid. No malicious relayer/validator/collusion is needed — a normal, honest relayer will trigger the payout unaware that governance intended to freeze the pallet.

### Recommendation
Add the same `ensure!(!Self::operating_mode().is_halted(), Error::<T>::Halted)` check at the top of `submit_delivery_receipt` (mirroring `inbound-queue`/`inbound-queue-v2::submit`), so that halting the outbound-queue-v2 pallet also blocks delivery-receipt processing and reward payout, not only new message sends.

### Proof of Concept
1. Governance calls `OutboundQueueV2::set_operating_mode(Root, BasicOperatingMode::Halted)` (this pallet's own storage, not the ethereum-client's).
2. Ethereum beacon light client (`pallet-ethereum-client`) remains in `Normal` mode.
3. A relayer submits `OutboundQueue::submit_delivery_receipt(origin, event)` with a valid receipt proof for an existing `PendingOrders` entry.
4. `T::Verifier::verify()` succeeds (beacon client not halted) because there is no local `ensure!(!Self::operating_mode().is_halted(), ...)` check in `submit_delivery_receipt`, unlike `inbound-queue`'s `submit`.
5. `process_delivery_receipt` runs to completion, calling `T::RewardPayment::register_reward` and removing the entry from `PendingOrders`, despite the outbound-queue-v2 pallet being halted.

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

**File:** bridges/snowbridge/pallets/inbound-queue/src/lib.rs (L237-243)
```rust
		pub fn submit(origin: OriginFor<T>, event: EventProof) -> DispatchResult {
			let who = ensure_signed(origin)?;
			ensure!(!Self::operating_mode().is_halted(), Error::<T>::Halted);

			// submit message to verifier for verification
			T::Verifier::verify(&event.event_log, &event.proof)
				.map_err(|e| Error::<T>::Verification(e))?;
```

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

**File:** bridges/snowbridge/pallets/ethereum-client/src/impls.rs (L21-41)
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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/test.rs (L390-416)
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
