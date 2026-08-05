Audit Report

## Title
Outbound Queue V2 has no operating-mode halt gate on message enqueue/delivery — governance cannot stop Snowbridge V2 outbound processing - ([File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs])

## Summary
`pallet-snowbridge-outbound-queue` (v1) enforces `ensure!(!Self::operating_mode().is_halted(), SendError::Halted)` in `SendMessage::deliver` before enqueuing, backed by a real `OperatingMode` storage item [1](#0-0) [2](#0-1) . The V2 outbound queue pallet declares an `Error::Halted` variant and an `Event::OperatingModeChanged`, implying an intended halt mechanism, but has no `OperatingMode` storage, no `set_operating_mode` call, and `deliver`/`do_process_message` never check any halt state before enqueuing/processing messages [3](#0-2) [4](#0-3) .

## Finding Description
I traced the only caller of `outbound-queue-v2::SendMessage` on the BridgeHub side, `snowbridge-pallet-system-v2::Pallet::send`, and confirmed it performs no halt check either. `set_operating_mode` in `system-v2` only constructs `Command::SetOperatingMode` and sends it as an Ethereum-bound message via `Self::send(...)` — this changes the **Ethereum Gateway contract's** operating mode, not any local BridgeHub storage [5](#0-4) . There is no local-side halt storage anywhere in `system-v2`, `outbound-queue-v2`, or the shared `SendMessage` trait definition [6](#0-5) . `BasicOperatingMode`/`is_halted()` is defined generically in `snowbridge-core` and is actually wired up only in v1's outbound queue and in `pallet-ethereum-client` (verifier halt) [7](#0-6) . The related fix in `prdoc/stable2603-2/pr_11856.prdoc` confirms this gap is real and was only partially closed: it explicitly states that halting `pallet-ethereum-client` only blocks `inbound_queue_v2::submit` / `outbound_queue_v2::submit_delivery_receipt` (verification-gated paths), and does *not* mention or cover the `deliver`/enqueue path for new outbound messages via `system-v2::send` → `outbound-queue-v2::deliver` [8](#0-7) . So even after that fix, new outbound message enqueuing/processing (`do_process_message`, nonce assignment, `PendingOrder` creation, merkle-root commitment into the header digest) in v2 has no governance-controlled halt gate on the BridgeHub side.

## Impact Explanation
This matches the "public underpriced work that degrades block production or stalls bridge processing" / permanent bridge-state-lock class: since `outbound-queue-v2` has no `OperatingMode` storage and no check in `deliver`/`do_process_message`, governance has no BridgeHub-side primitive to halt further outbound message queuing, nonce assignment, `PendingOrder` creation with fee liability, or merkle-root commitment into the header digest during an incident — regardless of what is done to `pallet-ethereum-client`'s verifier, which only affects the receipt-verification path, not new message emission. This is a genuine missing invariant relative to v1's design intent (evidenced by the unused `Error::Halted` and `Event::OperatingModeChanged` vestiges left in the v2 pallet).

## Likelihood Explanation
Medium: this requires no attacker action — it manifests automatically whenever BridgeHub governance needs to halt outbound v2 processing (e.g., a compromised sibling-chain sender or Gateway-side incident) and discovers there is no storage/extrinsic in `outbound-queue-v2` or `system-v2` to do so locally, unlike v1. Ordinary sibling-parachain XCM sends and `system-v2` calls continue to flow through `deliver` → `do_process_message` unimpeded.

## Recommendation
Add an `OperatingMode` (or reuse `BasicOperatingMode`) storage item to `pallet-snowbridge-outbound-queue-v2`, plus a governance-gated `set_operating_mode`/halt extrinsic, and enforce `ensure!(!is_halted(), Error::Halted)` in `SendMessage::deliver` and/or at the start of `do_process_message`, exempting only the primary governance channel as v1 does.

## Proof of Concept
1. Deploy a runtime with `snowbridge-pallet-outbound-queue-v2` and `snowbridge-pallet-system-v2` configured normally.
2. Confirm no storage item or extrinsic exists to halt v2 outbound processing on BridgeHub (absent in both pallets' `lib.rs`); `system-v2::set_operating_mode` only emits an Ethereum-bound `Command::SetOperatingMode`, not a local halt [9](#0-8) .
3. Call any sibling-parachain XCM export or `system-v2::register_token`/`upgrade`; observe `Pallet::validate`/`deliver` in outbound-queue-v2 succeeds unconditionally and enqueues into `T::MessageQueue` [4](#0-3) .
4. Observe `do_process_message` assigns a nonce, inserts a `PendingOrder`, and `commit()` still writes the merkle root to the header digest at `on_finalize`, with no governance lever to stop this in v2 [10](#0-9) .

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue/src/send_message_impl.rs (L76-82)
```rust
	fn deliver(ticket: Self::Ticket) -> Result<H256, SendError> {
		let origin = AggregateMessageOrigin::Snowbridge(ticket.channel_id);

		if ticket.channel_id != PRIMARY_GOVERNANCE_CHANNEL {
			ensure!(!Self::operating_mode().is_halted(), SendError::Halted);
		}

```

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L237-240)
```rust
	/// The current operating mode of the pallet.
	#[pallet::storage]
	#[pallet::getter(fn operating_mode)]
	pub type OperatingMode<T: Config> = StorageValue<_, BasicOperatingMode, ValueQuery>;
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L217-223)
```rust
			count: u64,
		},
		/// Set OperatingMode
		OperatingModeChanged { mode: BasicOperatingMode },
		/// Delivery Proof received
		MessageDelivered { nonce: u64 },
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

**File:** bridges/snowbridge/pallets/system-v2/src/lib.rs (L184-200)
```rust
		/// Sends a message to the Gateway contract to change its operating mode
		///
		/// Fee required: No
		///
		/// - `origin`: Must be `GovernanceOrigin`
		#[pallet::call_index(1)]
		#[pallet::weight((<T as pallet::Config>::WeightInfo::set_operating_mode(), DispatchClass::Operational))]
		pub fn set_operating_mode(origin: OriginFor<T>, mode: OperatingMode) -> DispatchResult {
			let origin_location = T::GovernanceOrigin::ensure_origin(origin)?;
			let origin = Self::location_to_message_origin(origin_location)?;

			let command = Command::SetOperatingMode { mode };
			Self::send(origin, command, 0)?;

			Self::deposit_event(Event::<T>::SetOperatingMode { mode });
			Ok(())
		}
```

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/message.rs (L263-273)
```rust
pub trait SendMessage {
	type Ticket: Clone + Encode + Decode;

	/// Validate an outbound message and return a tuple:
	/// 1. Ticket for submitting the message
	/// 2. Delivery fee
	fn validate(message: &Message) -> Result<Self::Ticket, SendError>;

	/// Submit the message ticket for eventual delivery to Ethereum
	fn deliver(ticket: Self::Ticket) -> Result<H256, SendError>;
}
```

**File:** bridges/snowbridge/primitives/core/src/operating_mode.rs (L19-36)
```rust
pub enum BasicOperatingMode {
	/// Normal mode, when all operations are allowed.
	Normal,
	/// The pallet is halted. All non-governance operations are disabled.
	Halted,
}

impl Default for BasicOperatingMode {
	fn default() -> Self {
		Self::Normal
	}
}

impl BasicOperatingMode {
	pub fn is_halted(&self) -> bool {
		*self == BasicOperatingMode::Halted
	}
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
