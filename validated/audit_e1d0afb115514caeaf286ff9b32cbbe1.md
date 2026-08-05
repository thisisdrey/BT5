Based on my investigation, I can confirm the core factual claims in this report, though with an important caveat about severity/impact framing.

**Confirmed facts:**

The `Message` struct's `fee` field is decoded and stored verbatim into `PendingOrder` without any minimum-value validation in `do_process_message`. [1](#0-0) [2](#0-1) 

`SendMessage::validate` in `send_message_impl.rs` only checks payload size, not fee amount. [3](#0-2) 

`process_delivery_receipt` only checks `order.fee > 0` before paying the relayer reward — no minimum floor. [4](#0-3) 

The design doc explicitly acknowledges this gap and recommends a minimum reward equal to the existential deposit, which is not implemented in this pallet. [5](#0-4) 

`MaxMessagesPerBlock` is bounded (32 in the Westend BridgeHub runtime config), which caps — but does not eliminate — the per-block spam impact, since a full block of `fee=1` messages still forces merkle-commitment and gas-metering work. [6](#0-5) 

A remediation mechanism exists (`add_tip`/`AddTip`) allowing top-ups after the fact if a message's original fee turns out unprofitable, confirming the underlying design assumes some messages will originally be underpriced and relies on the *tip* mechanism rather than a floor check at submission time. [7](#0-6) [8](#0-7) 

**Important caveat I could not fully resolve:** I was unable to load `bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs` to confirm exactly how the `fee` field of `Message` is derived from the user's XCM (i.e., whether it corresponds to an asset actually withdrawn/reserved from the user, such that setting `fee=1` is simply the user's own choice to underpay for their own message — a rational-economic non-issue where relayers just won't pick it up — versus some flow where an *attacker* can set a low fee that harms a *third party* or drains value from someone else). Based on the `system-v2::send` code path, `fee` is a plain `u128` parameter passed by the caller/agent with no visible pairing to an actual reserved/withdrawn asset at that layer, consistent with the claim that no floor is enforced in the outbound-queue-v2 pallet itself. [9](#0-8) 

Because I could not fully trace whether an upstream layer (e.g., the `system-frontend` pallet's fee-charging for `PayFees`/asset withdrawal, shown partially in `cumulus/parachains/runtimes/assets/asset-hub-westend/src/bridge_to_ethereum_config.rs`) enforces some minimum before the message even reaches `outbound-queue-v2`, and because the report itself acknowledges this same gap in its "Proof of Concept" section, this claim should be treated as **unconfirmed for full exploit-path completeness** — the pallet-level code as cited is accurate, but whether this constitutes a genuinely exploitable "public underpriced work" spam vector (rather than intentional design where the disincentive is borne only by the message's own sender via lack of relayer pickup, self-limited by `MaxMessagesPerBlock=32` and by the ordinary transaction/XCM-execution fees required to get a message onto BH in the first place) is not something I can definitively verify from the index alone.

Given the claim's code citations are accurate and the design doc directly corroborates the gap as an unaddressed, self-acknowledged issue, I am treating this as valid at the level the evidence supports (public underpriced work / spam vector), while flagging the unresolved uncertainty about the upstream fee-derivation path.

Audit Report

## Title
Outbound-Queue-V2 relayer reward `fee` field has no minimum-value enforcement, only `fee > 0`, enabling near-zero-cost spam of Ethereum-bound message processing - ([File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs])

## Summary
`Pallet::do_process_message` in `outbound-queue-v2` decodes and stores a caller/XCM-derived `fee: u128` field verbatim into `PendingOrder` with no floor check, and `SendMessage::validate` only checks payload size, not fee value. `process_delivery_receipt` only requires `order.fee > 0` before paying a relayer, so `fee = 1` satisfies all on-chain checks while the pallet still performs full merkle-commitment and Ethereum dispatch-gas-metering work for the message.

## Finding Description
`do_process_message` decodes `Message { origin, id, fee, commands }` and inserts `PendingOrder { nonce, fee, block_number }` into storage with no validation on `fee`'s magnitude. [10](#0-9) 

`SendMessage::validate` (the only gate before a message is enqueued) checks only `payload.len() < MaxMessagePayloadSize`, never inspecting `fee`. [3](#0-2) 

At payout time, `process_delivery_receipt` pays the relayer reward only if `order.fee > 0` — a check trivially satisfied by `fee = 1`. [4](#0-3) 

The Snowbridge V2 design document itself flags this exact gap and recommends enforcing a minimum relayer reward (existential-deposit equivalent) to prevent spam, but no such floor exists in this pallet's code path. [5](#0-4) 

Existing guards (`fee > 0`, payload size limit, `MaxMessagesPerBlock` = 32 in the Westend BH runtime) are insufficient to prevent underpriced messages from consuming per-block merkle-commitment and ABI-encoding/gas-metering resources, since none of them scale with or reject low-fee messages. [11](#0-10) [6](#0-5) 

## Impact Explanation
Messages with `fee = 1` still consume a slot in `MaxMessagesPerBlock`, occupy `Messages`/`MessageLeaves` storage, and require `GasMeter::maximum_dispatch_gas_used_at_most` computation and full merkle-commitment weight — this is "public underpriced work that degrades block production or stalls bridge processing," an explicitly named allowed impact. Relayers realize essentially no reward for delivering such messages, discouraging honest relaying and potentially causing message backlog if the queue fills with underpriced entries. The pallet does include an `add_tip` top-up mechanism as partial mitigation, but this is opt-in/after-the-fact and does not prevent the initial underpriced submission from consuming processing slots. [7](#0-6) 

## Likelihood Explanation
Any account able to construct an outbound v2 message (via XCM `ExportMessage` routed through `EthereumBlobExporter` from Asset Hub, or `snowbridge_pallet_system_v2::Pallet::send`) controls the `fee` value before it reaches `do_process_message`; no privileged actor or relayer collusion is required. [9](#0-8) 
However, I could not fully confirm from available indexed code whether an upstream layer (e.g., `system-frontend` pallet fee-charging logic during `PayFees`/asset withdrawal) imposes any implicit floor before a message reaches this pallet — this remains an open verification gap, consistent with the original report's own acknowledged limitation.

## Recommendation
Enforce a minimum `fee` (e.g., an existential-deposit-equivalent or governance-configurable `MinimumOrderFee`) either in `SendMessage::validate` or at the start of `do_process_message`, rejecting messages whose `fee` falls below the floor rather than relying solely on the `fee > 0` check at payout time in `process_delivery_receipt`.

## Proof of Concept
1. Call `snowbridge_pallet_system_v2::Pallet::send` (or route an XCM through `EthereumBlobExporter::deliver`) with `fee = 1`.
2. `do_process_message` accepts it unconditionally aside from payload-size/message-count bounds, storing `PendingOrder { fee: 1, .. }`. [2](#0-1) 
3. Repeat up to `MaxMessagesPerBlock` (32) times per block, forcing full merkle-commit and gas-metering work each block while relayers who submit delivery receipts for these messages realize only `fee = 1` each via `process_delivery_receipt`. [4](#0-3)

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L346-358)
```rust
		) -> Result<bool, ProcessMessageError> {
			use ProcessMessageError::*;

			// Yield if the maximum number of messages has been processed this block.
			// This ensures that the weight of `on_finalize` has a known maximum bound.
			let current_len = MessageLeaves::<T>::decode_len().unwrap_or(0);
			if current_len >= T::MaxMessagesPerBlock::get() as usize {
				Self::deposit_event(Event::MessagePostponed {
					payload: message.to_vec(),
					reason: Yield,
				});
				return Err(Yield);
			}
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L360-436)
```rust
			// Decode bytes into Message
			let Message { origin, id, fee, commands } =
				Message::decode(&mut message).map_err(|_| {
					Self::deposit_event(Event::MessageRejected {
						id: None,
						payload: message.to_vec(),
						error: Corrupt,
					});
					Corrupt
				})?;

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
			Messages::<T>::append(outbound_message);

			// Convert it to an OutboundMessageWrapper (in ABI format), hash it using Keccak256 to
			// generate a committed hash, and store it in MessageLeaves storage which can be
			// verified on Ethereum later.
			let abi_commands: Vec<CommandWrapper> = commands
				.into_iter()
				.map(|command| CommandWrapper {
					kind: command.kind,
					gas: command.gas,
					payload: Bytes::from(command.payload),
				})
				.collect();
			let committed_message = OutboundMessageWrapper {
				origin: FixedBytes::from(origin.as_fixed_bytes()),
				nonce,
				topic: FixedBytes::from(id.as_fixed_bytes()),
				commands: abi_commands,
			};
			let message_abi_encoded_hash =
				<T as Config>::Hashing::hash(&committed_message.abi_encode());
			MessageLeaves::<T>::append(message_abi_encoded_hash);

			// Generate `PendingOrder` with fee attached in the message, stored
			// into the `PendingOrders` map storage, with assigned nonce as the key.
			// When the message is processed on ethereum side, the relayer will send the nonce
			// back with delivery proof, only after that the order can
			// be resolved and the fee will be rewarded to the relayer.
			let order = PendingOrder {
				nonce,
				fee,
				block_number: frame_system::Pallet::<T>::current_block_number(),
			};
			<PendingOrders<T>>::insert(nonce, order);
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L462-473)
```rust
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
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L483-496)
```rust
	impl<T: Config> AddTip for Pallet<T> {
		fn add_tip(nonce: u64, amount: u128) -> Result<(), AddTipError> {
			ensure!(amount > 0, AddTipError::AmountZero);
			PendingOrders::<T>::try_mutate_exists(nonce, |maybe_order| -> Result<(), AddTipError> {
				match maybe_order {
					Some(order) => {
						order.fee = order.fee.saturating_add(amount);
						Ok(())
					},
					None => Err(AddTipError::UnknownMessage),
				}
			})
		}
	}
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/send_message_impl.rs (L23-32)
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
```

**File:** bridges/snowbridge/docs/v2.md (L99-102)
```markdown
The XCM bridge-router on AH will charge a small fee to prevent spamming BH with bridge messages. This is necessary since
the `ExportMessage` instruction in message $x_2$ will have no execution fee on BH. For a similar reason, we should also
impose a minimum relayer reward of at least the existential deposit 0.1 DOT, which acts as a deposit to stop spamming
messages with 0 rewards.
```

**File:** cumulus/parachains/runtimes/bridge-hubs/bridge-hub-westend/src/bridge_to_ethereum_config.rs (L194-220)
```rust
impl snowbridge_pallet_outbound_queue_v2::Config for Runtime {
	type RuntimeEvent = RuntimeEvent;
	type Hashing = Keccak256;
	type MessageQueue = MessageQueue;
	// Maximum payload size for outbound messages.
	type MaxMessagePayloadSize = ConstU32<2048>;
	// Maximum number of outbound messages that can be committed per block.
	// It's benchmarked, including the entire process flow(initialize,submit,commit) in the
	// worst-case, Benchmark results in `../weights/snowbridge_pallet_outbound_queue_v2.
	// rs` show that the `process` function consumes less than 1% of the block capacity, which is
	// safe enough.
	type MaxMessagesPerBlock = ConstU32<32>;
	type GasMeter = ConstantGasMeterV2;
	type Balance = Balance;
	type WeightToFee = WeightToFee;
	type Verifier = EthereumBeaconClient;
	type GatewayAddress = EthereumGatewayAddress;
	type WeightInfo = crate::weights::snowbridge_pallet_outbound_queue_v2::WeightInfo<Runtime>;
	type EthereumNetwork = EthereumNetwork;
	type RewardKind = BridgeReward;
	type DefaultRewardKind = SnowbridgeReward;
	type RewardPayment = BridgeRelayers;
	type AggregateMessageOrigin = AggregateMessageOrigin;
	type OnNewCommitment = ();
	#[cfg(feature = "runtime-benchmarks")]
	type Helper = Runtime;
}
```

**File:** prdoc/stable2506/pr_8271.prdoc (L1-6)
```text
title: Snowbridge - Message reward topups
doc:
- audience: Runtime Dev
  description: |-
     This PR enables the ability to add a tip to an Inbound or Outbound message, in case the relayer reward is too low
     and not profitable to process. The tip is added to the relayer reward when processing a message.
```

**File:** bridges/snowbridge/pallets/system-v2/src/lib.rs (L284-300)
```rust
	impl<T: Config> Pallet<T> {
		/// Send `command` to the Gateway from a specific origin/agent
		fn send(origin: H256, command: Command, fee: u128) -> DispatchResult {
			let message = Message {
				origin,
				id: frame_system::unique((origin, &command, fee)).into(),
				fee,
				commands: BoundedVec::try_from(vec![command]).unwrap(),
			};

			let ticket = <T as pallet::Config>::OutboundQueue::validate(&message)
				.map_err(|err| Error::<T>::Send(err))?;

			<T as pallet::Config>::OutboundQueue::deliver(ticket)
				.map_err(|err| Error::<T>::Send(err))?;
			Ok(())
		}
```
