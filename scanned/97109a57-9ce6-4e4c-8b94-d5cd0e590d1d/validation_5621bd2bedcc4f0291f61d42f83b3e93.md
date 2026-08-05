### Title
Inbound Queue V2 marks Ethereum message nonce as processed before the XCM dispatch succeeds, permanently losing/locking messages on processor failure - (File: `bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs`)

### Summary
`Pallet::process_message` in the Snowbridge Inbound Queue V2 pallet sets the nonce bitmap ("message processed") **before** the actual external dispatch (XCM conversion + send to AssetHub) has succeeded. FRAME dispatchables are not automatically wrapped in a storage-transactional layer unless a pallet explicitly opts in with `#[transactional]`/`with_storage_layer`; this call performs no such wrapping. If the downstream "external call" (`T::MessageProcessor::process_message`, which converts the message to XCM and calls `SendXcm::deliver`) fails, the extrinsic returns `Err`, but the `Nonce::<T>::set(nonce)` write already committed to storage is not rolled back.

### Finding Description
```rust
// bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs
pub fn process_message(relayer: T::AccountId, message: Message) -> DispatchResult {
    ensure!(T::GatewayAddress::get() == message.gateway, Error::<T>::InvalidGateway);
    let (nonce, relayer_fee) = (message.nonce, message.relayer_fee);
    ensure!(!Nonce::<T>::get(nonce), Error::<T>::InvalidNonce);

    // Mark message as received
    Nonce::<T>::set(nonce);                                  // <-- marker advances first

    let message_id = T::MessageProcessor::process_message(relayer.clone(), message)
        .map_err(|e| match e { ... })?;                      // <-- external call, unchecked path leaves nonce set on Err
    ...
}
``` [1](#0-0) 

`T::MessageProcessor::process_message` is implemented by `XcmMessageProcessor::process_xcm`, which is the genuine "external call" analog to the report: it converts the message and then calls `Executor::charge_fees` and `Sender::deliver(ticket)` — an outbound handoff whose failure modes (`ConvertMessage`, `SendMessage`, `ProcessMessage`) are all surfaced only as `Err` after the nonce bit has already been persisted: [2](#0-1) [3](#0-2) 

The nonce bitmap is a `StorageMap` explicitly documented as tracking "whether a specific nonce has been processed or not," used to reject replays: [4](#0-3) 

Because `submit`/`process_message` are not wrapped in `#[transactional]`/`with_storage_layer`, a storage write performed before a `?`-propagated error is not automatically discarded in FRAME — that guarantee only exists for pallets that explicitly request it. No such wrapping exists on this call path: [5](#0-4) 

This is the exact class of bug in the external report: an "external call" (the outbound XCM send/dispatch to AssetHub) whose failure is not validated *before* advancing durable protocol state (the nonce/replay marker), so the state machine proceeds as if the call succeeded.

### Impact Explanation
Any Ethereum-originated message whose downstream XCM conversion or send fails for a reason unrelated to the Merkle proof (e.g. `InvalidAsset`, `CannotReanchor`, `InvalidNetwork`, `SendFailure`/`Unreachable` due to channel congestion or fee misconfiguration on Bridge Hub) gets its nonce permanently marked as processed even though the funds/XCM were never delivered to the beneficiary. Since `ensure!(!Nonce::<T>::get(nonce), Error::<T>::InvalidNonce)` unconditionally rejects any further `submit` for that nonce, the message can never be resubmitted or retried by any relayer. This is a permanent bridge-state lock and fund loss for the intended beneficiary on the Polkadot side, matching the "permanent user-fund or bridge-state lock" and "message queues... must only advance after decode, dispatch, execution, and settlement succeed atomically" impact gates.

### Likelihood Explanation
`submit` is a public, unprivileged extrinsic (`ensure_signed`), callable by any relayer once a valid Ethereum event/proof exists; no malicious relayer, validator, or governance action is required — only a message whose payload triggers a processor-side failure (asset/network/reanchor/fee/channel condition), which is realistically reachable given user-supplied token IDs, claimers, and remote XCM content embedded in the Ethereum-side message.

### Recommendation
Only persist `Nonce::<T>::set(nonce)` after `T::MessageProcessor::process_message` returns `Ok`, or wrap `process_message` in `frame_support::transactional`/`with_storage_layer` so that any error path fully reverts the nonce marker, allowing the message to be resubmitted/retried instead of being silently and permanently dropped.

### Proof of Concept
1. Relayer submits a valid, proof-verified Ethereum event whose `Message` decodes successfully and passes gateway/nonce checks.
2. `Nonce::<T>::set(nonce)` executes, marking the nonce processed.
3. `Converter::convert` or `Sender::deliver` inside `XcmMessageProcessor::process_xcm` fails (e.g., `ConvertMessageError::CannotReanchor` for a foreign asset, or `SendError::NotApplicable`/`Fees` due to channel/fee state) — `process_message` returns `Err(...)`.
4. The `submit` extrinsic fails and is recorded as failed in the block, but the earlier `Nonce::<T>::set(nonce)` storage write persists (no transactional wrapper to roll it back).
5. Any later `submit` call for the same nonce is rejected via `Error::<T>::InvalidNonce`, permanently preventing delivery of the funds/message tied to that nonce. [6](#0-5)

### Citations

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs (L165-168)
```rust
	/// StorageMap used for encoding a SparseBitmapImpl that tracks whether a specific nonce has
	/// been processed or not. Message nonces are unique and never repeated.
	#[pallet::storage]
	pub type NonceBitmap<T: Config> = StorageMap<_, Twox64Concat, u64, u128, ValueQuery>;
```

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs (L181-198)
```rust
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

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs (L214-232)
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
```

**File:** bridges/snowbridge/primitives/inbound-queue/src/v2/processor.rs (L54-73)
```rust
	pub fn process_xcm(
		who: T::AccountId,
		message: Message,
	) -> Result<XcmHash, MessageProcessorError> {
		// Convert the message to XCM
		let xcm = Converter::convert(message).map_err(|error| {
			tracing::error!(target: LOG_TARGET, ?error, "XCM conversion failed with error");
			MessageProcessorError::ConvertMessage(error)
		})?;

		// Forward XCM to a target location
		let dest = TargetLocation::get();
		let message_id = Self::send_xcm(dest.clone(), &who, xcm.clone()).map_err(|error| {
			tracing::error!(target: LOG_TARGET, ?error, ?dest, ?xcm, "XCM send failed with error");
			MessageProcessorError::SendMessage(error)
		})?;

		// Return the message_id
		Ok(message_id)
	}
```

**File:** bridges/snowbridge/primitives/inbound-queue/src/v2/processor.rs (L86-109)
```rust
	fn send_xcm(
		dest: Location,
		fee_payer: &T::AccountId,
		xcm: Xcm<()>,
	) -> Result<XcmHash, SendError> {
		let fee_payer = AccountToLocation::try_convert(fee_payer).map_err(|err| {
			tracing::error!(
				target: LOG_TARGET,
				?err,
				"Failed to convert account to XCM location",
			);
			SendError::NotApplicable
		})?;
		let (ticket, fee) = validate_send::<Sender>(dest, xcm)?;
		Executor::charge_fees(fee_payer, fee).map_err(|error| {
			tracing::error!(
				target: LOG_TARGET,
				?error,
				"Charging fees failed with error",
			);
			SendError::Fees
		})?;
		Sender::deliver(ticket)
	}
```
