### Title
Snowbridge Outbound Queue V2 accepts user-controlled `fee` and `gas` values with no on-chain minimum check, allowing underpriced messages to consume the shared per-block Ethereum delivery capacity - ([File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs])

### Summary
The external report's core defect is a fixed/unvalidated probe value (100-token swap) used to sanity-check economic value, which breaks down and can be gamed when the real cost of the underlying operation diverges from what the check assumes. The local analog is in Snowbridge's V2 outbound queue: unlike the V1 pipeline, which computes an on-chain fee floor from `GasMeter::maximum_gas_used_at_most()` and governance `PricingParameters` before a message is admitted (`calculate_fee` in `bridges/snowbridge/pallets/outbound-queue/src/lib.rs:368-393`, enforced in `send_message_impl.rs`'s `validate()`), the V2 pipeline's `SendMessage::validate` no longer returns or enforces any `Fee` at all (`bridges/snowbridge/primitives/outbound-queue/src/v2/message.rs:263-273`). The `fee` field committed to storage is taken verbatim from the XCM the user constructs (`bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs:307-317`), and the real Ethereum-side gas requirement for `Command::CallContract` is likewise taken directly from user input (`gas: gas_limit` in `ConstantGasMeterV2::maximum_dispatch_gas_used_at_most`, `bridges/snowbridge/primitives/outbound-queue/src/v2/message.rs:291-306`). Nothing in `do_process_message` (`bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs:346-443`) checks that `fee` is proportional to, or even non-zero relative to, the declared `gas`.

### Finding Description
In V1, admission of a message to the outbound queue is gated by an on-chain-computed fee: [1](#0-0) 
This fee is derived from `GasMeter::maximum_gas_used_at_most(command)` (a fixed, per-command constant, `bridges/snowbridge/primitives/outbound-queue/src/v1/message.rs:340-376`) multiplied by governance-set `fee_per_gas`/`multiplier`/`exchange_rate`, and is returned from `validate()` so the caller is forced to pay it.

In V2, the documented design goal was to move fee estimation off-chain (dry-running) and "eliminate the on-chain exchange rate" (`bridges/snowbridge/docs/v2.md:246-249`, and the module doc in `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs:1-42`). Concretely:
- `SendMessage::validate` for V2 only returns a `Ticket`, with no `Fee` computed or enforced on-chain: [2](#0-1) 
- The `fee` stored in the `Message` is whatever `fee_amount` the XCM converter extracted from the user's own XCM instructions (i.e., the remote fee asset amount the user chose to attach), not a value derived from actual command cost: [3](#0-2) 
- `do_process_message` takes this untouched `fee` and stores it directly into `PendingOrder`, with the real per-command gas computed from `T::GasMeter::maximum_dispatch_gas_used_at_most`, and no comparison between the two: [4](#0-3) 
- Crucially, for `Command::CallContract` the "maximum dispatch gas" is not a protocol-fixed constant like the other commands — it is taken directly from user-supplied `gas_limit`: [5](#0-4) 

This is the same broken invariant as the audit report: a value meant to act as an economic sanity check (fee vs. real execution cost) is either a user-supplied number taken at face value (the `fee`) or itself derived from an unrelated, unbounded user input (`gas_limit`), so there is no guarantee that fee ever covers cost. In the original report, the fixed "100 token" swap probe could be starved of liquidity or made irrelevant for high-value tokens; here, the "gas × fee_per_gas" cost model that made V1 safe is simply absent for V2's admission path, so a caller can set `fee` arbitrarily low (including 0 — no `ensure!(fee > 0)` is present in `do_process_message`) while requesting a `CallContract` with a large `gas` requirement.

### Impact Explanation
`MaxMessagesPerBlock` bounds how many outbound messages can be committed per block (`ConstU32<32>` in `bridge_to_ethereum_config.rs:179-220`) and is shared across all channels/users. Because admission has no fee floor, an unprivileged user can flood the queue with `CallContract`/other-command messages carrying `fee = 0` (or negligible), each consuming one of the 32 per-block slots. Relayers have no economic incentive to relay these (the reward comes from `fee`, claimed via `submit_delivery_receipt`/`process_delivery_receipt`), so they are never delivered, yet they permanently occupy `PendingOrders` storage and consume the fixed per-block processing budget that legitimate, well-paying messages must compete for. This directly matches the Impact Gate's "public underpriced work that degrades block production or stalls bridge processing": the bridge's fixed committing capacity is monopolized by worthless work, starving real cross-chain transfers and governance/system messages that share the same `MaxMessagesPerBlock` limit, and bloating `PendingOrders` with orders that can never be resolved.

### Likelihood Explanation
This requires only an unprivileged user able to send an XCM through the normal `ExportMessage`/`InitiateTransfer` path to Bridge Hub — the same path any ordinary user uses to bridge assets to Ethereum, no relayer/validator/governance/admin action needed. The only uncertainty is whether an additional fee-floor check exists upstream (e.g., in `snowbridge_pallet_system_v2::Pallet::send` or `EthereumBlobExporter::deliver`) that I was not able to fully inspect within the available context; if such a check exists it would mitigate this specific path, but no such enforcement was found in the outbound-queue-v2 pallet itself, which is the final admission point before enqueuing.

### Recommendation
Reinstate an on-chain fee floor for V2 messages analogous to V1's `calculate_fee`: compute a minimum required fee from `GasMeter::maximum_dispatch_gas_used_at_most(command)` (bounding user-supplied `gas` for `CallContract` to a sane maximum) times a governance-controlled `fee_per_gas`, and reject (`ensure!`) any message in `do_process_message`/`validate` whose attached `fee` is below that floor, mirroring the check that already protects V1.

### Proof of Concept
1. Attacker constructs an XCM on Asset Hub with `InitiateTransfer` targeting Ethereum, including a `Transact`/`CallContract`-style remote instruction with an arbitrary `gas` value (e.g., a large `gas_limit`) and attaches a `remote_fees` asset of amount `1` (or as low as the XCM barrier allows) rather than a realistic estimate.
2. This XCM is exported via `EthereumBlobExporter`/`XcmConverter`, which builds a V2 `Message` whose `fee` field equals the attacker-chosen remote fee amount (`convert.rs:307-317`), with no on-chain check against the command's real gas cost.
3. `OutboundQueue::validate` (V2) accepts the ticket without computing/enforcing any `Fee` (`v2/message.rs:263-273`), and `deliver` enqueues it.
4. `do_process_message` stores the message and a `PendingOrder{fee, ...}` using the attacker-supplied `fee` (`outbound-queue-v2/src/lib.rs:346-443`), consuming one of the `MaxMessagesPerBlock` slots.
5. Repeating this in bulk across blocks fills the fixed per-block commit capacity with unprofitable messages that no rational relayer will deliver, degrading throughput for legitimate bridge traffic while `PendingOrders` accumulates unresolved entries indefinitely.

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L366-393)
```rust
		/// Calculate total fee in native currency to cover all costs of delivering a message to the
		/// remote destination. See module-level documentation for more details.
		pub(crate) fn calculate_fee(
			gas_used_at_most: u64,
			params: PricingParameters<T::Balance>,
		) -> Fee<T::Balance> {
			// Remote fee in ether
			let fee = Self::calculate_remote_fee(
				gas_used_at_most,
				params.fee_per_gas,
				params.rewards.remote,
			);

			// downcast to u128
			let fee: u128 = fee.try_into().defensive_unwrap_or(u128::MAX);

			// multiply by multiplier and convert to local currency
			let fee = FixedU128::from_inner(fee)
				.saturating_mul(params.multiplier)
				.checked_div(&params.exchange_rate)
				.expect("exchange rate is not zero; qed")
				.into_inner();

			// adjust fixed point to match local currency
			let fee = Self::convert_from_ether_decimals(fee);

			Fee::from((Self::calculate_local_fee(), fee))
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

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/message.rs (L291-306)
```rust
impl GasMeter for ConstantGasMeter {
	fn maximum_dispatch_gas_used_at_most(command: &Command) -> u64 {
		match command {
			Command::SetOperatingMode { .. } => 40_000,
			Command::Upgrade { initializer, .. } => {
				// total maximum gas must also include the gas used for updating the proxy before
				// the the initializer is called.
				50_000 + initializer.maximum_required_gas
			},
			Command::UnlockNativeToken { .. } => 200_000,
			Command::RegisterForeignToken { .. } => 1_200_000,
			Command::MintForeignToken { .. } => 100_000,
			Command::CallContract { gas: gas_limit, .. } => *gas_limit,
		}
	}
}
```

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs (L307-317)
```rust
		ensure!(commands.len() > 0, NoCommands);

		// ensure SetTopic exists
		let topic_id = match_expression!(self.next()?, SetTopic(id), id).ok_or(SetTopicExpected)?;

		let message = Message {
			id: (*topic_id).into(),
			origin,
			fee: fee_amount,
			commands: BoundedVec::try_from(commands).map_err(|_| TooManyCommands)?,
		};
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L346-443)
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

			<Nonce<T>>::set(nonce);

			Self::deposit_event(Event::MessageAccepted { id, nonce });

			Ok(true)
		}
```
