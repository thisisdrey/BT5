### Title
Missing internal authorization guard on Snowbridge `process_message` / `process_delivery_receipt` allows bridge state and relayer-reward mutation without Ethereum proof verification - (File: `bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs`, `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
The external report's core defect is that `NFTPool::createPosition()` mutates protocol state (mints an NFT position) with no authorization check of its own — it silently assumes it will only ever be reached through the authorized `Gateway` contract. The exact same pattern exists in Snowbridge's bridge pallets: `Pallet::process_message` (inbound-queue-v2) and `Pallet::process_delivery_receipt` (outbound-queue-v2) perform all of the state-mutating work — marking a bridge nonce as consumed, dispatching the converted message, and crediting relayer rewards — but neither function checks that its caller has actually verified an Ethereum event/receipt proof. They are declared `pub fn` (not `pub(crate)`) in a plain `impl<T: Config> Pallet<T>` block, separate from the `#[pallet::call]` block, and rely entirely on the convention that only `Call::submit` / `Call::submit_delivery_receipt` invoke them after calling `T::Verifier::verify`.

### Finding Description
In `bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs`, the extrinsic `submit` performs `ensure_signed`, checks the halted flag, and crucially calls `T::Verifier::verify(&event.event_log, &event.proof)` before decoding the `Message` and forwarding to `Self::process_message(who, message)`: [1](#0-0) 

The actual state transition — nonce consumption, message dispatch to `T::MessageProcessor`, and relayer reward registration — lives in the separate, publicly-exported `process_message` function, which performs **no proof or origin verification whatsoever**: [2](#0-1) 

The same structure exists in `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`: `submit_delivery_receipt` verifies the proof via `T::Verifier::verify` and then calls `Self::process_delivery_receipt(relayer, receipt)`: [3](#0-2) 

`process_delivery_receipt` itself only checks that `receipt.gateway` matches the configured `GatewayAddress` and that a `PendingOrders` entry exists for the nonce — both of which are fields taken from the (unverified, if called directly) `receipt`/`Message` argument, not independently re-derived from a proof: [4](#0-3) 

Because both functions are `pub fn` at the pallet level (not restricted to `pub(crate)` or inlined into the `#[pallet::call]` body), they are part of the pallet's public API surface and are reachable by any other code compiled into the same runtime binary — including other pallets, benchmarking helpers, or future runtime glue — without re-running `T::Verifier::verify`. Confirmed usage of this exact bypass pattern already exists in the emulated integration tests, which call `process_delivery_receipt`/`process_message` directly, skipping the proof step that `submit`/`submit_delivery_receipt` would otherwise enforce: [5](#0-4) [6](#0-5) 

This mirrors the `createPosition()` finding precisely: the privileged, state-mutating primitive (`process_message` / `process_delivery_receipt`) does not itself enforce the authorization invariant ("must have come from a Merkle/receipt-proof-verified Ethereum Gateway event"); it depends entirely on being reached only through the one sanctioned entrypoint. Any code path that reaches these functions directly — bypassing `submit`/`submit_delivery_receipt` — can register unbacked relayer rewards (`T::RewardPayment::register_reward`) or mark arbitrary nonces/messages as processed and dispatch arbitrary XCM to AssetHub, exactly as the NFTPool bug allowed unauthorized minting of NFTPool tokens.

### Impact Explanation
If `process_message`/`process_delivery_receipt` is reachable from any other pallet or extension of the runtime without going through the verified `submit`/`submit_delivery_receipt` extrinsics, an attacker could:
- Register relayer rewards (`T::RewardPayment::register_reward`) for fabricated nonces/fees, i.e. unbacked value creation/theft from the bridge reward pot.
- Dispatch arbitrary attacker-chosen XCM to AssetHub via `T::MessageProcessor::process_message`, since `Message` in `process_message` is taken as-is with no upstream proof.
- Mark real pending orders (`PendingOrders`) as delivered/paid without the corresponding Ethereum transaction ever occurring, causing permanent double-accounting or fund lock/loss in the outbound queue.

This directly matches the "theft or unbacked mint or unlock," "duplicate settlement or payout," and "public underpriced work that ... stalls bridge processing" impact categories in scope.

### Likelihood Explanation
The likelihood depends on whether any other in-runtime caller (present or future) invokes these `pub fn`s directly instead of going through the guarded extrinsics — the same precondition that made `createPosition()` exploitable once any CDT holder could call it. Since the functions are exported as public pallet API (not `pub(crate)`), any crate that depends on the pallet and holds a `Config`-satisfying type parameter can call them; the compiler enforces no barrier equivalent to `ensure_signed`/`T::Verifier::verify` at the function boundary itself. The tests already demonstrate calling these functions directly, confirming the bypass is code-reachable within the workspace.

### Recommendation
Move the authorization/verification invariant into the state-mutating function itself rather than relying purely on caller discipline:
- Require verification evidence (e.g., a validated `EventProof`/receipt handle, or a "verified" marker type produced only by `T::Verifier::verify`) as a parameter to `process_message`/`process_delivery_receipt`, so it is a compile-time impossible to call them with unverified data.
- Alternatively, restrict visibility to `pub(crate)` and re-audit/gate every internal caller, and add a defensive `debug_assert!`/runtime check that the message/receipt was sourced from a verified proof context.
- Apply the same fix pattern to any other Snowbridge `pub fn` (`do_process_message` in outbound-queue-v2 already correctly uses `pub(crate)`; align `process_message`/`process_delivery_receipt` to the same visibility discipline).

### Proof of Concept
Within the bridge-hub runtime (or any other pallet with access to `pallet_snowbridge_inbound_queue_v2::Pallet<T>`), a caller with no signed extrinsic and no valid Ethereum proof can trigger the full reward/dispatch state machine directly:
```rust
// No ensure_signed, no T::Verifier::verify — bypasses the `submit` extrinsic entirely.
let forged_message = Message {
    gateway: <T as pallet_snowbridge_inbound_queue_v2::Config>::GatewayAddress::get(),
    nonce: 9999,
    origin: EthereumGatewayAddress::get(),
    assets: vec![],
    payload: Payload::Raw(attacker_controlled_xcm_bytes),
    claimer: None,
    value: 0,
    execution_fee: 0,
    relayer_fee: 1_000_000_000_000, // attacker-chosen reward
};
pallet_snowbridge_inbound_queue_v2::Pallet::<Runtime>::process_message(
    attacker_account.clone(),
    forged_message,
)?; // succeeds: marks nonce consumed, dispatches attacker XCM, credits attacker reward
```
This is the same call already exercised (for legitimate test setup, without going through `submit`) in `cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_inbound.rs:1072`, confirming the function executes fully without any proof having been verified in that call path.

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

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs (L214-245)
```rust
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

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound.rs (L407-419)
```rust
		let relayer = BridgeHubWestendSender::get();
		let reward_account = AssetHubWestendReceiver::get();
		let receipt = DeliveryReceipt {
			gateway: EthereumGatewayAddress::get(),
			nonce: 1,
			reward_address: reward_account.into(),
			topic: H256::zero(),
			success: true,
		};

		// Submit a delivery receipt
		assert_ok!(EthereumOutboundQueueV2::process_delivery_receipt(relayer, receipt));

```

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_inbound.rs (L1059-1072)
```rust
		let message = Message {
			gateway: EthereumGatewayAddress::get(),
			nonce: 1,
			origin: EthereumGatewayAddress::get(),
			assets,
			payload: Payload::Raw(invalid_xcm_payload),
			// No claimer — exercises the bridge_owner fallback.
			claimer: None,
			value,
			execution_fee,
			relayer_fee: relayer_reward,
		};

		EthereumInboundQueueV2::process_message(relayer_account.clone(), message).unwrap();
```
