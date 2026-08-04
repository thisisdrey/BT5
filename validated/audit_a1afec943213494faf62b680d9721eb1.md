### Title
Unbounded, XCM‑controlled `gas` on `Command::CallContract` breaks the fixed‑gas invariant enforced for every other Snowbridge V2 outbound command - (File: `bridges/snowbridge/primitives/outbound-queue/src/v2/message.rs`)

### Summary
The external report's core invariant is: **fields that cross the Rust/Solidity boundary in a bridge message must be bound and validated identically on both sides; an unvalidated or mismatched field lets one side accept data the other side never intended to authorize.** The Snowbridge V2 outbound queue reproduces this class of bug locally: for every command type, the gas charged/committed to Ethereum is a fixed protocol constant computed by `ConstantGasMeter`, except for `Command::CallContract`, where the `gas` value is taken verbatim from data supplied by an ordinary, unprivileged XCM `Transact` instruction and forwarded unmodified into the committed outbound message that the Ethereum `Gateway` contract will later execute against.

### Finding Description
`Command::CallContract` carries a `gas: u64` field: [1](#0-0) 

This command is built directly from an XCM `Transact` instruction decoded from arbitrary caller-supplied bytes, with the `gas` value taken as-is: [2](#0-1) 

For every other command, `ConstantGasMeter::maximum_dispatch_gas_used_at_most` returns a fixed protocol constant (40_000 / 50_000+initializer / 200_000 / 1_200_000 / 100_000), deliberately preventing the submitter from influencing the gas that will be committed to Ethereum. But for `CallContract` it simply returns the caller-controlled value unmodified: [3](#0-2) 

That returned value becomes the top-level `gas` field of `OutboundCommandWrapper`/`CommandWrapper`, which is committed into the Merkle leaf hash (`MessageLeaves`) and delivered to the Ethereum `Gateway` contract for execution — this is the exact code path that assembles and commits the message: [4](#0-3) 

The Solidity `CommandWrapper` struct treats `gas` as the dispatch gas limit forwarded to the target call on Ethereum: [5](#0-4) 

This is structurally the same class of defect as the report: one side of the bridge (the Rust `ConstantGasMeter`/message-assembly logic) is supposed to enforce a fixed, protocol-controlled value for a wire field, but for the `CallContract` branch it instead passes through an attacker-supplied value unchecked, so the invariant "gas commitments to Ethereum are protocol-bound, not attacker-bound" silently breaks for one specific command — just as the original report found one specific message (`TransferAssets`, `UpdateCentrifugeGasPrice`, etc.) where the encoded/decoded parameters diverged from the intended, validated format.

### Impact Explanation
Because `gas` for `CallContract` is fully attacker-controlled and is not bounded against the fee actually paid (`fee_amount` is extracted independently from `PayFees`/`WithdrawAsset` in `extract_remote_fee`, with no linkage back to the requested `gas`), any account able to construct a valid V2 XCM with `AliasOrigin` allowed by `AllowedAliasOrigin` can:
- request an arbitrarily large `gas` value that gets committed into the message hash and delivered on-chain to Ethereum for execution, while paying a fee sized for ordinary transfers, i.e., public underpriced work being pushed onto relayers/Gateway execution; or
- cause the committed message's gas requirement to exceed what the relayer/Gateway can economically or practically execute, stalling delivery of that message (and, since nonces are sequential, potentially subsequent messages) on the bridge.

This falls squarely in the accepted impact category of "public underpriced work that degrades block production or stalls bridge processing," and does not require a malicious relayer, validator, or governance actor — only an ordinary XCM submitter able to route a `Transact` instruction through the V2 converter.

### Likelihood Explanation
The `Transact` path is reachable by any origin permitted by `AllowedAliasOrigin` to alias — this is a general-purpose XCM feature, not a privileged path, and `ContractCall::V1 { gas, .. }` is decoded straight from Transact call bytes with no explicit cap. The asymmetry between the constant-gas branches and the pass-through `CallContract` branch in the same `match` block (`bridges/snowbridge/primitives/outbound-queue/src/v2/message.rs:291-306`) makes the missing bound easy to spot and straightforward to exploit for anyone who can format a `Transact` instruction accepted by the XCM converter.

### Recommendation
Cap the `gas` value accepted for `Command::CallContract` (analogous to the fixed constants used for other commands), and/or require the fee (`fee_amount`) supplied in `PayFees`/`WithdrawAsset` to scale with the requested `gas`, mirroring the recommendation in the external report to keep message parameters consistent and validated identically on both sides of the bridge.

### Proof of Concept
1. Construct a V2 XCM message with the standard `WithdrawAsset`/`PayFees` sequence using a minimal fee, followed by `AliasOrigin` (satisfying `AllowedAliasOrigin`), `DepositAsset`, and a `Transact` instruction whose encoded call decodes (via `ContractCall::decode_all`) to `ContractCall::V1 { target, calldata, gas: u64::MAX, value }`.
2. Submit this XCM so it reaches `XcmConverter::convert` (`bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs:294-305`), which pushes `Command::CallContract { gas: u64::MAX, .. }` into the resulting `Message` without any bound check.
3. When the outbound queue pallet processes the message (`bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs:360-424`), `ConstantGasMeter::maximum_dispatch_gas_used_at_most` returns the attacker-supplied `u64::MAX` gas as the committed `CommandWrapper.gas`, which is hashed into `MessageLeaves` and queued for relay/execution on Ethereum — despite the minimal fee paid in step 1.

### Citations

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/message.rs (L24-40)
```rust
	sol! {
		struct OutboundMessageWrapper {
			// origin
			bytes32 origin;
			// Message nonce
			uint64 nonce;
			// Topic
			bytes32 topic;
			// Commands
			CommandWrapper[] commands;
		}

		struct CommandWrapper {
			uint8 kind;
			uint64 gas;
			bytes payload;
		}
```

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/message.rs (L182-192)
```rust
	/// Call Contract on Ethereum
	CallContract {
		/// Target contract address
		target: H160,
		/// ABI-encoded calldata
		calldata: Vec<u8>,
		/// Maximum gas to forward to target contract
		gas: u64,
		/// Include ether held by agent contract
		value: u128,
	},
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

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs (L294-305)
```rust
		// Transact commands
		let transact_call = match_expression!(self.peek(), Ok(Transact { call, .. }), call);
		if let Some(transact_call) = transact_call {
			let _ = self.next();
			let transact =
				ContractCall::decode_all(&mut transact_call.clone().into_encoded().as_slice())
					.map_err(|_| TransactDecodeFailed)?;
			match transact {
				ContractCall::V1 { target, calldata, gas, value } => commands
					.push(Command::CallContract { target: target.into(), calldata, gas, value }),
			}
		}
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L360-424)
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
```
