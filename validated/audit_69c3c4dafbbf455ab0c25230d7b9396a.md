## Analysis

The Frax finding's core invariant is: **a query/validation function that is supposed to reflect the paused/disabled state of an operation must not silently report success when the operation is actually disabled.** The concrete local analog is the Snowbridge outbound-queue's `SendMessage` implementation, where `validate()` never checks the halted operating mode while `deliver()` does.### Title
`SendMessage::validate` for the Snowbridge outbound queue never checks the halted operating mode, unlike `deliver` - (File: `bridges/snowbridge/pallets/outbound-queue/src/send_message_impl.rs`)

### Summary
The Frax report's core defect is that a spec-mandated *query* function (`maxDeposit`/`maxMint`) must reflect the paused/disabled state without reverting, but instead always reported full capacity, misleading callers into thinking the operation was available. The local analog is `snowbridge_pallet_outbound_queue`'s `SendMessage::validate`, which computes a fee/ticket and always returns `Ok` regardless of whether the pallet's operating mode is halted, while `deliver` (called immediately afterward) does check `Self::operating_mode().is_halted()` and can reject the very ticket that `validate` just approved.

### Finding Description
`SendMessage` for the outbound queue pallet is implemented as: [1](#0-0) 
which validates payload size and channel existence and computes the fee, but performs no check of the pallet's halted state. [2](#0-1) 
`deliver`, by contrast, explicitly gates on `!Self::operating_mode().is_halted()` for any channel other than the primary governance channel, and only then enqueues the message.

This means `validate()` — the "can I do this and how much will it cost" check analogous to `maxDeposit`/`maxMint` — systematically misreports availability: it returns `Ok` (a valid ticket + non-zero fee quote) even when the queue is halted, exactly as `maxDeposit`/`maxMint` returned `uint128.max` even when the Frax pair was paused. The actual gating only happens one step later in `deliver`.

This asymmetry is consumed directly by `EthereumBlobExporter::validate`, which calls `OutboundQueue::validate(&outbound_message)` to build the exported message and fee for an XCM `ExportMessage` instruction: [3](#0-2) 
The generic XCM `send_xcm` helper explicitly documents the intended pattern of calling `validate` first (to get price and decide whether to pay) and `deliver` second: [4](#0-3) 
Under this design, callers (asset-hub/bridge-hub XCM programs that `WithdrawAsset`/`BuyExecution`/`PayFees` for the bridging fee before the `ExportMessage` instruction executes) rely on `validate` succeeding as a signal that delivery will also succeed. Because `validate` does not check `is_halted()`, that signal is wrong whenever the bridge is halted: the fee-paying instructions in the XCM program already executed and consumed/withdrew the user's fee assets before the executor reaches `ExportMessage` → `deliver`, which then fails with `SendError::Halted`.

### Impact Explanation
When the outbound queue is halted (an operating-mode state that can be toggled through the bridge's system pallet, e.g. during incident response or governance-driven maintenance), any user-initiated Ethereum-bound transfer or XCM program that pays bridging fees before the `ExportMessage` instruction will:
1. Have its fee assets withdrawn/burned via prior `WithdrawAsset`/`BuyExecution`/`PayFees` instructions (these effects are not undone by a later instruction failure), and
2. Fail at the `ExportMessage`/`deliver` step, with no message ever queued to Ethereum.

This produces value loss or, at best, forces users to identify and manually reclaim trapped assets via `ClaimAsset` — an availability/fund-safety break that matches "permanent user-fund or bridge-state lock" and "public underpriced work that … stalls bridge processing" impact categories: users keep submitting (and paying gas/fees for) transactions that `validate` reports as fine, but that are silently unroutable while halted, and no query-time signal exists to prevent this from happening repeatedly at scale.

### Likelihood Explanation
This requires no privileged actor, malicious relayer, or governance abuse to trigger — any ordinary unprivileged user attempting a bridge transfer while the queue happens to be halted (a legitimate, foreseeable operational state, not an attacker precondition) hits this path deterministically. The halted state itself is a normal part of the bridge's incident-response design (see `PRIMARY_GOVERNANCE_CHANNEL` bypass and `is_halted()` check), so the divergence between `validate` and `deliver` is guaranteed to be exercised during any real halt event, not a rare edge case.

### Recommendation
Make `validate()` mirror the same halted-state gate that `deliver()` enforces (excluding the governance channel bypass, consistent with `deliver`), returning `SendError::Halted` (or an equivalent non-committal error) from `validate` itself so that callers — and any XCM program paying fees ahead of `ExportMessage` — never treat a halted queue as routable. This aligns the pallet with the same "queries must reflect actual availability" principle from EIP-4626 that the original report was about, and prevents fee-paying instructions in front of `ExportMessage` from executing against a channel that is guaranteed to fail at delivery.

### Proof of Concept
1. Bridge operators halt the outbound queue via the system pallet's operating-mode call (a normal, documented incident-response action, not an attacker action).
2. A user on AssetHub submits a token transfer to Ethereum, which is translated into an XCM program of the form `WithdrawAsset` (fee) → `BuyExecution`/`PayFees` → `WithdrawAsset` (transfer assets) → `DepositAsset`/`ExportMessage`.
3. During XCM execution, the executor reaches `ExportMessage`, which calls `EthereumBlobExporter::validate` → `OutboundQueue::validate`, per [3](#0-2) ; this succeeds and returns a ticket + fee, because `validate` in [1](#0-0)  performs no halted check.
4. The executor calls `deliver`, per [2](#0-1) , which now evaluates `Self::operating_mode().is_halted()` as true and returns `SendError::Halted`, aborting the `ExportMessage` instruction.
5. The prior `WithdrawAsset`/`BuyExecution`/`PayFees` instructions in the same XCM program already executed before the failing `ExportMessage` step; their asset movements are not rolled back by the later failure, so the user's fee (and potentially the principal transfer assets, depending on program structure) end up trapped/lost while no message is ever delivered to Ethereum — reproducing, at message-queue-quote granularity, the exact "always-available, never-zero, ignore-pause" defect described in the Frax `maxDeposit`/`maxMint` report.

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue/src/send_message_impl.rs (L41-49)
```rust
	fn validate(
		message: &Message,
	) -> Result<(Self::Ticket, Fee<<Self as SendMessageFeeProvider>::Balance>), SendError> {
		// The inner payload should not be too large
		let payload = message.command.abi_encode();
		ensure!(
			payload.len() < T::MaxMessagePayloadSize::get() as usize,
			SendError::MessageTooLarge
		);
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

**File:** bridges/snowbridge/primitives/outbound-queue/src/v1/converter/mod.rs (L126-138)
```rust
		let outbound_message = Message { id: Some(message_id.into()), channel_id, command };

		// validate the message
		let (ticket, fee) = OutboundQueue::validate(&outbound_message).map_err(|err| {
			tracing::error!(target: "xcm::ethereum_blob_exporter", error=?err, "OutboundQueue validation of message failed.");
			SendError::Unroutable
		})?;

		// convert fee to Asset
		let fee = Asset::from((Location::parent(), fee.total())).into();

		Ok(((ticket.encode(), message_id), fee))
	}
```

**File:** polkadot/xcm/src/v4/traits.rs (L296-311)
```rust
/// Convenience function for using a `SendXcm` implementation. Just interprets the `dest` and wraps
/// both in `Some` before passing them as mutable references into `T::send_xcm`.
///
/// Returns either `Ok` with the price of the delivery, or `Err` with the reason why the message
/// could not be sent.
///
/// Generally you'll want to validate and get the price first to ensure that the sender can pay it
/// before actually doing the delivery.
pub fn send_xcm<T: SendXcm>(
	dest: Location,
	msg: Xcm<()>,
) -> result::Result<(XcmHash, Assets), SendError> {
	let (ticket, price) = T::validate(&mut Some(dest), &mut Some(msg))?;
	let hash = T::deliver(ticket)?;
	Ok((hash, price))
}
```
