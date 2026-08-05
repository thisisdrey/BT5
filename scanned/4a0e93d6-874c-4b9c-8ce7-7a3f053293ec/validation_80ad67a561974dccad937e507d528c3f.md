### Title
Outbound-Queue-v2 delivery fee omits base-gas/verification overhead present in v1's `GasMeter`, causing systematic fee under-estimation that can strand Ethereum-bound messages - ([File: bridges/snowbridge/primitives/outbound-queue/src/v2/message.rs])

### Summary
The external report's root cause is that a fee-estimation function builds its estimate from an incomplete representation of the data that will actually be sent/executed, silently dropping one required component and thereby under-pricing the operation. The same class of bug exists in Snowbridge's outbound-queue-v2 pricing path: the `GasMeter` trait used to price messages to Ethereum in v2 no longer includes the fixed "base gas" component (transaction overhead, calldata cost, message-verification cost) that v1's `GasMeter` explicitly adds via `MAXIMUM_BASE_GAS`, and the v2 outbound-queue pallet never recomputes/validates the `fee` field against `PricingParameters` on-chain the way v1 does — it simply stores whatever `fee` was supplied in the `Message`.

### Finding Description
In v1, `snowbridge_outbound_queue_primitives::v1::GasMeter` defines: [1](#0-0) 
with `MAXIMUM_BASE_GAS: u64 = 185_000` explicitly documented to cover "21_000 transaction cost, roughly worst case 64_000 for calldata, and 100_000 for message verification": [2](#0-1) 
This base cost is unconditionally added to every command's dispatch gas via `maximum_gas_used_at_most`, and the resulting total is passed into `Pallet::calculate_fee`, which converts it into a native-currency fee used to pay relayers: [3](#0-2) [4](#0-3) 

In v2, the equivalent trait dropped the base-gas concept entirely — it only defines `maximum_dispatch_gas_used_at_most`, with no `MAXIMUM_BASE_GAS`/`maximum_gas_used_at_most` counterpart: [5](#0-4) 
The v2 outbound-queue pallet's `do_process_message` uses this trait only to compute the per-command `gas` field that is committed for Ethereum execution, and it takes the delivery `fee` directly from the decoded `Message` without ever recalculating or validating it against `PricingParameters`/gas cost: [6](#0-5) 
`SendMessage::validate` in v2 likewise only checks payload size, and does not compute or verify a fee at all: [7](#0-6) 

This mirrors the report's core defect: the value used to price/estimate the operation (`dataSend`/gas total) is built from an incomplete set of the actual cost-determining variables — in v1 the base gas (tx + calldata + verification) is a first-class, unconditionally-included term; in v2 it has silently disappeared from the trait, and nothing in the pallet enforces a floor. Whatever fee accompanies a v2 `Message` (set upstream by `pallet-system-v2`/the XCM converter) is trusted as-is and stored into `PendingOrder.fee`, which is what a relayer will eventually be paid for completing delivery and submitting the delivery receipt.

### Impact Explanation
If the upstream fee-setting logic for v2 messages (mirroring the same omission the external report describes) does not separately account for the fixed Ethereum-side transaction/verification overhead, the `reward` embedded in `PendingOrder` can be insufficient to make relaying profitable or even to cover gas. Because Ethereum gas prices fluctuate and v2 has no on-chain re-derivation/floor check of the fee at `do_process_message` time, underpriced messages can sit uncollected, degrading/stalling bridge message processing — this falls under the accepted "public underpriced work that degrades block production or stalls bridge processing" impact category.

### Likelihood Explanation
Likelihood is high in the same sense as the original report: this is not a malicious-actor-triggered path, it is a systemic pricing gap that fires on ordinary usage of the v2 outbound queue (`Upgrade`, `CallContract`, `UnlockNativeToken`, etc.) any time the upstream fee-setter's estimate uses only per-command dispatch gas without adding the fixed base overhead that v1 always included. Because outbound-queue-v2's `do_process_message` never validates the committed `fee` against `PricingParameters`/gas, there is no on-chain guard that would catch or correct an underestimate before the message is irrevocably committed to the Merkle root and queued for relaying.

### Recommendation
Reinstate a base-gas (or equivalent fixed overhead) component in `snowbridge_outbound_queue_primitives::v2::GasMeter`, analogous to v1's `MAXIMUM_BASE_GAS`, and have `pallet-outbound-queue-v2::do_process_message` (or `validate`) recompute/validate the `Message.fee` against `PricingParameters` and the full gas total (base + per-command dispatch), rejecting or topping-up messages whose supplied fee is below the on-chain-computed minimum, rather than trusting the caller-supplied `fee` verbatim.

### Proof of Concept
1. Compare `GasMeter` trait definitions: v1 (`bridges/snowbridge/primitives/outbound-queue/src/v1/message.rs:317-330,340-376`) includes `MAXIMUM_BASE_GAS = 185_000` added on top of every command's dispatch gas.
2. v2 (`bridges/snowbridge/primitives/outbound-queue/src/v2/message.rs:275-306`) has no such base-gas field/method at all — only `maximum_dispatch_gas_used_at_most`.
3. In `outbound-queue-v2::do_process_message` (`bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs:371-436`), `T::GasMeter::maximum_dispatch_gas_used_at_most` is used purely to populate each command's `gas` field for on-chain commitment; the `fee` field placed into `PendingOrder` is taken directly from the decoded `Message` with no recomputation or floor check.
4. `SendMessage::validate` for v2 (`bridges/snowbridge/pallets/outbound-queue-v2/src/send_message_impl.rs:23-32`) only checks `payload.len()` — it performs no fee computation/validation whatsoever, unlike v1's `validate` which calls `Self::calculate_fee(gas_used_at_most, T::PricingParameters::get())`.
5. Consequently, any upstream fee estimate for a v2 message that (like the reported LayerZero bug) omits a real cost component (here, fixed base/verification/tx overhead) flows unchecked into `PendingOrder.fee`, risking relayer underpayment and stalled delivery, with no on-chain safeguard to catch it.

### Citations

**File:** bridges/snowbridge/primitives/outbound-queue/src/v1/message.rs (L317-330)
```rust
pub trait GasMeter {
	/// All the gas used for submitting a message to Ethereum, minus the cost of dispatching
	/// the command within the message
	const MAXIMUM_BASE_GAS: u64;

	/// Total gas consumed at most, including verification & dispatch
	fn maximum_gas_used_at_most(command: &Command) -> u64 {
		Self::MAXIMUM_BASE_GAS + Self::maximum_dispatch_gas_used_at_most(command)
	}

	/// Measures the maximum amount of gas a command payload will require to *dispatch*, NOT
	/// including validation & verification.
	fn maximum_dispatch_gas_used_at_most(command: &Command) -> u64;
}
```

**File:** bridges/snowbridge/primitives/outbound-queue/src/v1/message.rs (L342-348)
```rust
impl GasMeter for ConstantGasMeter {
	// The base transaction cost, which includes:
	// 21_000 transaction cost, roughly worst case 64_000 for calldata, and 100_000
	// for message verification
	const MAXIMUM_BASE_GAS: u64 = 185_000;

	fn maximum_dispatch_gas_used_at_most(command: &Command) -> u64 {
```

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

**File:** bridges/snowbridge/pallets/outbound-queue/src/send_message_impl.rs (L59-60)
```rust
		let gas_used_at_most = T::GasMeter::maximum_gas_used_at_most(&message.command);
		let fee = Self::calculate_fee(gas_used_at_most, T::PricingParameters::get());
```

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/message.rs (L275-306)
```rust
pub trait GasMeter {
	/// Measures the maximum amount of gas a command payload will require to *dispatch*, NOT
	/// including validation & verification.
	fn maximum_dispatch_gas_used_at_most(command: &Command) -> u64;
}

/// A meter that assigns a constant amount of gas for the execution of a command
///
/// The gas figures are extracted from this report:
/// > forge test --match-path test/Gateway.t.sol --gas-report
///
/// A healthy buffer is added on top of these figures to account for:
/// * The EIP-150 63/64 rule
/// * Future EVM upgrades that may increase gas cost
pub struct ConstantGasMeter;

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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L371-436)
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
