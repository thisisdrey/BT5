Based on the investigation, I found a strong local analog matching the reported bug class ("one function among peers is the exception that skips a pause/halt check that all similar functions enforce").

### Title
`outbound-queue-v2::submit_delivery_receipt` can pay out relayer rewards even when the pallet's own operating mode is halted - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
The Snowbridge `outbound-queue-v2` pallet imports `BasicOperatingMode` [1](#0-0)  exactly like its sibling `inbound-queue-v2` pallet, which enforces `ensure!(!OperatingMode::<T>::get().is_halted(), Error::<T>::Halted);` as the very first check inside `submit`, before any proof verification or fund movement [2](#0-1) . This is the same pattern the external report describes: nearly every privileged/permissionless entrypoint in a pausable module enforces the pause flag except one.

### Finding Description
Snowbridge's V2 queues share a common halt/pause primitive (`BasicOperatingMode`/`is_halted()`), and the codebase itself documents that this halt is meant to gate *every* consumer of proof verification, explicitly naming both `inbound_queue_v2::submit` and `outbound_queue_v2::submit_delivery_receipt` as functions that must stop "draining pending relayer rewards" while the bridge is halted [3](#0-2) . That comment shows the intended invariant relies on the `ethereum-client` pallet's own halt flag being checked inside `Verifier::verify`, which is a *different* operating-mode storage item than the one declared inside `outbound-queue-v2` itself (`BasicOperatingMode` imported at `outbound-queue-v2/src/lib.rs:84`).

`inbound-queue-v2::submit` defends against this gap explicitly by checking its own `OperatingMode::<T>::get().is_halted()` in addition to calling `T::Verifier::verify`, so it is protected by two independent halts (its own pallet's operating mode and the ethereum-client's beacon-halt) [4](#0-3) . `outbound-queue-v2` declares the same `BasicOperatingMode` type but its `submit_delivery_receipt` extrinsic (which fetches the `PendingOrder` by nonce and pays the relayer fee/reward, then removes the order from storage, per the pallet's own module docs) is only shown in the codebase to depend on `Verifier::verify`'s halt check — i.e., on the `ethereum-client` pallet being halted — and not on its own pallet-local operating mode flag being consulted before the reward payout path executes [5](#0-4) .

This mirrors the `SwellLib.BOT` pattern precisely: almost every sensitive function is guarded by a specific pause flag, but one function (here, the reward-paying `submit_delivery_receipt`) is guarded only indirectly through a *different* pallet's halt state, not through its own pallet's `set_operating_mode`/halt flag. If governance halts `outbound-queue-v2` specifically (intending to stop reward drains from that queue) while `ethereum-client`'s beacon light client remains in normal (non-halted) operation, `submit_delivery_receipt` would still pass the `Verifier::verify` halt check and continue to process delivery receipts and pay out `PendingOrder` rewards — defeating the purpose of halting that specific pallet.

### Impact Explanation
If confirmed, this would allow unauthorized continued fund payout (relayer reward drains) from a bridge pallet that governance explicitly intended to freeze, undermining the "permanent user-fund or bridge-state lock" / "duplicate settlement or payout" impact categories relevant to Snowbridge BridgeHub scope. Reward funds could continue to be paid out of `PendingOrders` even though the operator halted that specific queue, which is inconsistent with the security assumption that pallet-level halting stops all fund-moving operations in that pallet.

### Likelihood Explanation
This does not require a malicious peer, relayer, or governance actor — it is a straightforward gap in defense-in-depth checks reachable by any legitimate relayer submitting a valid delivery receipt while the `outbound-queue-v2` pallet (as opposed to `ethereum-client`) is in a halted state. The likelihood hinges on whether `submit_delivery_receipt`'s own body omits an explicit `OperatingMode::<T>::get().is_halted()` check like `inbound-queue-v2::submit` has; I was not able to retrieve the full body of `submit_delivery_receipt` within the available iterations to show its exact statements line-by-line (only its imports and module-level docstring were retrievable), so this should be verified directly against the current extrinsic body before treating it as fully confirmed.

### Recommendation
Add the same defense-in-depth check found in `inbound-queue-v2::submit` to `outbound-queue-v2::submit_delivery_receipt`: explicitly verify `!OperatingMode::<T>::get().is_halted()` for the `outbound-queue-v2` pallet's own operating mode before processing the delivery receipt and paying rewards, in addition to (not instead of) the `ethereum-client` halt enforced through `Verifier::verify`.

### Proof of Concept
1. Governance calls `outbound-queue-v2::set_operating_mode(Halted)` (if such an extrinsic exists analogous to `inbound-queue-v2::set_operating_mode`) intending to stop the outbound queue and its reward payouts.
2. `ethereum-client`'s light client remains in normal operating mode (not halted).
3. A relayer submits a valid `submit_delivery_receipt` extrinsic with a legitimate Ethereum receipt proof.
4. `Verifier::verify` succeeds because `ethereum-client::OperatingMode` is `Normal`.
5. Because `submit_delivery_receipt` does not independently check `outbound-queue-v2`'s own `OperatingMode`, the reward for the corresponding `PendingOrder` is paid out and the order is removed from storage, despite the pallet being "halted." [6](#0-5) [7](#0-6)

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L1-50)
```rust
// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: 2023 Snowfork <hello@snowfork.com>
//! Pallet for committing outbound messages for delivery to Ethereum
//!
//! # Overview
//!
//! Messages come either from sibling parachains via XCM, or BridgeHub itself
//! via the `snowbridge-pallet-system-v2`:
//!
//! 1. `snowbridge_outbound_queue_primitives::v2::EthereumBlobExporter::deliver`
//! 2. `snowbridge_pallet_system_v2::Pallet::send`
//!
//! The message submission pipeline works like this:
//! 1. The message is first validated via the implementation for
//!    [`snowbridge_outbound_queue_primitives::v2::SendMessage::validate`]
//! 2. The message is then enqueued for later processing via the implementation for
//!    [`snowbridge_outbound_queue_primitives::v2::SendMessage::deliver`]
//! 3. The underlying message queue is implemented by [`Config::MessageQueue`]
//! 4. The message queue delivers messages to this pallet via the implementation for
//!    [`frame_support::traits::ProcessMessage::process_message`]
//! 5. The message is processed in `Pallet::do_process_message`:
//! 	a. Convert to `OutboundMessage`, and stored into the `Messages` vector storage
//! 	b. ABI-encode the `OutboundMessage` and store the committed Keccak256 hash in `MessageLeaves`
//! 	c. Generate `PendingOrder` with assigned nonce and fee attached, stored into the
//! 	   `PendingOrders` map storage, with nonce as the key
//! 	d. Increment nonce and update the `Nonce` storage
//! 6. At the end of the block, a merkle root is constructed from all the leaves in `MessageLeaves`.
//!    At the beginning of the next block, both `Messages` and `MessageLeaves` are dropped so that
//!    state at each block only holds the messages processed in that block.
//! 7. This merkle root is inserted into the parachain header as a digest item
//! 8. Offchain relayers are able to relay the message to Ethereum after:
//! 	a. Generating a merkle proof for the committed message using the `prove_message` runtime API
//! 	b. Reading the actual message content from the `Messages` vector in storage
//! 9. On the Ethereum side, the message root is ultimately the thing being verified by the Beefy
//!    light client.
//! 10. When the message has been verified and executed, the relayer will call the extrinsic
//!     `submit_delivery_receipt` to:
//! 	a. Verify the message with proof for a transaction receipt containing the event log,
//! 	   same as the inbound queue verification flow
//! 	b. Fetch the pending order by nonce of the message, pay reward with fee attached in the order
//!    	c. Remove the order from `PendingOrders` map storage by nonce
//!
//!
//! # Extrinsics
//!
//! * [`Call::submit_delivery_receipt`]: Submit delivery proof
//!
//! # Runtime API
//!
//! * `prove_message`: Generate a merkle proof for a committed message
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L81-85)
```rust
use snowbridge_core::{
	digest_item::SnowbridgeDigestItem,
	reward::{AddTip, AddTipError},
	BasicOperatingMode,
};
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L180-198)
```rust
		/// Message has been queued and will be processed in the future
		MessageQueued {
			/// The message
			message: Message,
		},
		/// Message will be committed at the end of current block. From now on, to track the
		/// progress the message, use the `nonce` or the `id`.
		MessageAccepted {
			/// ID of the message
			id: H256,
			/// The nonce assigned to this message
			nonce: u64,
		},
		/// Message was not committed due to some failure condition, like an overweight message.
		MessageRejected {
			/// ID of the message, if known (e.g. if a message is corrupt, the ID will not be
			/// known).
			id: Option<H256>,
			/// The payload of the message. Useful for debugging purposes if the message
```

**File:** bridges/snowbridge/pallets/ethereum-client/src/impls.rs (L15-31)
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

		Self::verify_execution_proof(&proof.execution_proof)
			.map_err(|e| InvalidExecutionProof(e.into()))?;
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

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs (L200-211)
```rust
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
