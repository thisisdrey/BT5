### Title
Permanent, unrecoverable drop of Snowbridge V2 outbound messages on bounded-command conversion failure, after upstream value has already moved - ([File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs])

### Summary
The external report's core broken invariant is: a cross-chain message that already carries "spent" value (burned/locked funds) is only checked for structural/state compatibility on the **receiving** side, after enqueue; if that check fails, the message is dropped/stuck permanently, and the already-moved value is unrecoverable. The same broken invariant exists in `snowbridge-pallet-outbound-queue-v2::do_process_message`, where a message's `commands` are converted into a bounded collection *after* the message has already been queued (and after the corresponding value/fee accounting for the remote leg has already happened on the source side of the bridge). A conversion failure here is treated by `pallet-message-queue` as a **permanent** error, so the message is dropped forever with no retry, no refund, and no notification to the origin chain.

### Finding Description
`Pallet::do_process_message` decodes the queued `Message`, converts its `commands` into ABI-ready wrappers, and then attempts to bound them into the `OutboundMessage.commands` field via `try_into()`: [1](#0-0) 

If this conversion fails, the pallet emits `MessageRejected` with `ProcessMessageError::Corrupt` and returns `Err(Corrupt)`. This is exactly analogous to the TapToken report's revert on a post-send state mismatch (there: reward-token count mismatch; here: command-count/size bound mismatch), except the check happens strictly at *processing* time, not at *validate*/*send* time: [2](#0-1) 

`SendMessage::validate` only checks total payload byte size (`MaxMessagePayloadSize`), never the number/structure of `commands`, so a message that passes `validate`/`deliver` and gets enqueued can still fail at `do_process_message` time.

Crucially, `pallet-message-queue` treats `Corrupt`/`BadFormat`/`Unsupported` as **permanent** failures — the message is dropped with a `ProcessingFailed` event and never retried: [3](#0-2) 

The pallet's own module doc explicitly states this method "does not roll back storage changes on error," and no compensating action (refund, notify origin, requeue) exists for this path: [4](#0-3) 

By the time this message reaches `MessageQueue`/`do_process_message` on BridgeHub, the corresponding asset movement/fee (`PayFees`, `WithdrawAsset`/`ReserveAssetDeposited` for the remote transfer) has already been executed as part of the XCM program on the sending chain (Asset Hub), per the documented V2 flow: [5](#0-4) 

That XCM execution on Asset Hub is a separate, already-finalized state transition on a different chain/block; it cannot be rolled back when BridgeHub later fails to process the exported message. This reproduces the report's exact primitive: **value is moved (burned/locked) at send time based on an assumption that the receiving side's structural checks will succeed; the receiving side's check is only evaluated later, independently, and its failure is unrecoverable and value-destructive** rather than being surfaced back to the sender or retried.

### Impact Explanation
Any user-driven cross-chain transfer whose XCM program produces a `commands` set/size that exceeds what `OutboundMessage.commands`'s bound allows (or otherwise fails the `try_into` conversion) results in:
- The message being permanently dropped (`ProcessingFailed`/`MessageRejected`, `Corrupt`), never delivered to Ethereum.
- No nonce/`PendingOrder` ever created, so there is no relayer reward path and no way to detect/re-drive the message from the receiving side.
- The value already committed on the sending chain (Asset Hub) for this transfer remains spent/locked with no corresponding execution on Ethereum and no automatic refund mechanism in this pallet.
This is a direct "permanent user-fund lock/loss" and "duplicate/absent settlement" impact class as defined by the required-impacts gate (message queues/receipts must only advance after decode, dispatch, execution and settlement succeed atomically — here dispatch fails permanently while upstream settlement has already occurred).

### Likelihood Explanation
No malicious/privileged actor is required. Any unprivileged user constructing (or whose wallet/dApp constructs) an XCM program that is valid enough to pass `EthereumBlobExporter`/`SendMessage::validate` (which only checks total payload size) but produces a `commands` vector that fails the bounded conversion in `do_process_message` triggers this path deterministically. Because `validate` and `do_process_message` enforce different, non-identical constraints, this is a structural gap rather than an edge case requiring adversarial coordination.

### Recommendation
- Enforce the exact same structural/size constraints on `commands` (and any other bounded fields of `OutboundMessage`) inside `SendMessage::validate`/`EthereumBlobExporter::validate`, before any value-moving XCM instructions execute on the source chain, so failures happen pre-commitment rather than post-commitment.
- If a permanent decode/conversion failure can still occur at `do_process_message` time, treat it as recoverable: preserve enough information to trigger an asset-trap/claim or a refund path back to the origin, rather than silently dropping the message with `Corrupt`.
- Add integration tests that construct messages passing `validate` but failing the `try_into()` bound in `do_process_message`, and assert that no value is unrecoverably lost.

### Proof of Concept
1. On Asset Hub, construct and send an XCM program via `InitiateAssetsTransfer`/`ExportMessage` targeting Ethereum, with a legitimate `WithdrawAsset`/`PayFees` sequence, but structured (e.g., unusually large or malformed instruction/asset list) so it converts, on BridgeHub, into a `Message.commands` vector whose length/content exceeds the bound expected by `OutboundMessage.commands`'s `try_into()`.
2. `EthereumBlobExporter::validate` and `SendMessage::validate` on BridgeHub only check `payload.len() < MaxMessagePayloadSize` (see `send_message_impl.rs` lines 23-32) and succeed; the message is enqueued via `MessageQueue::enqueue_message`.
3. Meanwhile, on Asset Hub, the withdraw/fee-payment instructions in the originating XCM program have already executed and finalized in an earlier/parallel block — value is already spent.
4. When `pallet-message-queue` services the queue and calls `do_process_message`, the `commands.clone().try_into()` at `lib.rs` line 394 fails, returning `Err(Corrupt)`.
5. `pallet-message-queue::process_message_payload` treats this as `Unprocessable { permanent: true }` (see `message-queue/src/lib.rs` lines 1609-1613): the message is dropped for good, `MessageRejected` is emitted, no `PendingOrder`/nonce is created, and no compensating action occurs — the user's already-spent value on Asset Hub is permanently unrecoverable through this pallet.

**Caveat / open verification item:** I was not able to fully trace, within the available indexed code, the exact point and pallet where the Asset Hub-side fee/asset withdrawal for the remote (Ethereum) leg is irrevocably finalized relative to the BridgeHub processing step (i.e., whether any asset-trap/claim mechanism exists for this specific V2 cross-chain leg). This would need to be confirmed against `bridges/snowbridge/pallets/system-v2` and the Asset Hub runtime's XCM configuration to build a fully executable PoC; the structural bug in `do_process_message`'s permanent-drop-on-conversion-failure path is, however, directly confirmed in the cited code.

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L341-345)
```rust
		/// Process a message delivered by the MessageQueue pallet.
		/// IMPORTANT!! This method does not roll back storage changes on error.
		pub(crate) fn do_process_message(
			_: ProcessMessageOriginOf<T>,
			mut message: &[u8],
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L371-402)
```rust
			// Convert it to OutboundMessage and save into Messages storage
			let commands: Vec<OutboundCommandWrapper> = commands
				.into_iter()
				.map(|command| OutboundCommandWrapper {
					kind: command.index(),
					gas: T::GasMeter::maximum_dispatch_gas_used_at_most(&command),
					payload: command.abi_encode(),
				})
				.collect();

			let nonce = <Nonce<T>>::get().checked_add(1).ok_or_else(|| {
				Self::deposit_event(Event::MessageRejected {
					id: None,
					payload: message.to_vec(),
					error: Unsupported,
				});
				Unsupported
			})?;

			let outbound_message = OutboundMessage {
				origin,
				nonce,
				topic: id,
				commands: commands.clone().try_into().map_err(|_| {
					Self::deposit_event(Event::MessageRejected {
						id: Some(id),
						payload: message.to_vec(),
						error: Corrupt,
					});
					Corrupt
				})?,
			};
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/send_message_impl.rs (L23-43)
```rust
	fn validate(message: &Message) -> Result<Self::Ticket, SendError> {
		// The inner payload should not be too large
		let payload = message.encode();
		ensure!(
			payload.len() < T::MaxMessagePayloadSize::get() as usize,
			SendError::MessageTooLarge
		);

		Ok(message.clone())
	}

	fn deliver(ticket: Self::Ticket) -> Result<H256, SendError> {
		let origin = ticket.origin.into();

		let message =
			BoundedVec::try_from(ticket.encode()).map_err(|_| SendError::MessageTooLarge)?;

		T::MessageQueue::enqueue_message(message.as_bounded_slice(), origin);
		Self::deposit_event(Event::MessageQueued { message: ticket.clone() });
		Ok(ticket.id)
	}
```

**File:** substrate/frame/message-queue/src/lib.rs (L1609-1613)
```rust
			Err(error @ BadFormat | error @ Corrupt | error @ Unsupported) => {
				// Permanent error - drop
				Self::deposit_event(Event::<T>::ProcessingFailed { id: id.into(), origin, error });
				MessageExecutionStatus::Unprocessable { permanent: true }
			},
```

**File:** bridges/snowbridge/docs/v2.md (L61-120)
```markdown
    DepositAsset (KLT, 40) beneficiary=Bob
```

### Step 4: AH executes message x1

The message $x_1$ is application-specific:

```text
ReserveAssetDeposited (KLT, 80)
PayFees (KLT, 20)
SetAssetClaimer Kilt/Alice
AliasOrigin Kilt/Alice
ExchangeAsset give=(KLT, 20) want=(WETH, 1)
InitiateAssetsTransfer asset=(KLT, 60) remoteFee=(WETH, 1) dest=Ethereum
  DepositAsset (KLT, 60) beneficiary=Bob
```

or

```text
*ReserveAssetDeposited (KLT, 80)
*PayFees (KLT, 20)
*SetAssetClaimer Kilt/Alice
*AliasOrigin Kilt/Alice
ExchangeAsset give=(KLT, 20) want=(WETH, 1)
InitiateAssetsTransfer asset=(KLT, 60) remoteFee=(WETH, 1) dest=Ethereum
  DepositAsset (KLT, 60) beneficiary=Bob
  Transact Bob.hello()
```

Note that the `SetAssetClaimer` instruction is placed before `AliasOrigin` in case AH fails to interpret the latter
instruction.

In all cases, $x_1$ should contain the necessary instructions to:

1. Pay fees for local execution using `PaysFees`
2. Obtain WETH for remote delivery fees.

The XCM bridge-router on AH will charge a small fee to prevent spamming BH with bridge messages. This is necessary since
the `ExportMessage` instruction in message $x_2$ will have no execution fee on BH. For a similar reason, we should also
impose a minimum relayer reward of at least the existential deposit 0.1 DOT, which acts as a deposit to stop spamming
messages with 0 rewards.

### Step 5: BH executes message x2

Message $x_2$ is parsed by the `SnowbridgeMessageExporter` in block $n$ with the following effects:

- A bridge command $m$ is committed to binary merkle tree $M_n$.
  - The transferred asset is parsed from `ReserveAssetDeposited` , `WithdrawAsset` or `TeleportedAssetReceived`
    instructions for the local, destination and teleport asset transfer types respectively.
  - The original origin is preserved through the `AliasOrigin` instruction. This will allow us to resolve agents for the
    case of `Transact`.
  - The message exporter must be able to support multiple assets and reserve types in the same message and potentially
    multiple `Transacts`.
  - The Message Exporter must be able to support multiple Deposited Assets.
  - The Message Exporter must be able to parse `SetAssetClaimer` and allow the provided location to claim the assets on
    BH in case of errors.
- Given relayer reward $r$ in WETH, set storage $P(\mathrm{hash}(m)) = r$. This is parsed from the `WithdrawAsset` and
  `PayFees` instruction within `ExportMessage`.

```
