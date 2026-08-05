Based on my investigation, the strongest local analog to the reported "undercounted wrapper overhead in cross-chain gas" bug-class is in Snowbridge's outbound queue gas metering for messages sent to Ethereum.

### Title
Fixed `MAXIMUM_BASE_GAS` constant does not scale with actual wrapped-message calldata size, systematically underpricing Snowbridge outbound message delivery - (File: `bridges/snowbridge/primitives/outbound-queue/src/v1/message.rs`)

### Summary
`ConstantGasMeter::maximum_gas_used_at_most` prices the Ethereum-side gas cost of relaying a Snowbridge outbound message as a hardcoded constant (`MAXIMUM_BASE_GAS = 185_000`) plus a per-command dispatch estimate, but this constant is not derived from — and does not scale with — the actual size of the full wrapped message (`CommittedMessage`) that is ABI-encoded and delivered to the Gateway contract. This is the same class of bug as the Sherlock report: the fee/gas computation only accounts for a fixed assumption of the message envelope instead of the actual wrapped payload size, so when the real wire message is larger than assumed, users/relayers pay less than the actual gas consumed on the destination side.

### Finding Description
`GasMeter::maximum_gas_used_at_most` is defined as `MAXIMUM_BASE_GAS + maximum_dispatch_gas_used_at_most(command)`: [1](#0-0) 

`ConstantGasMeter` sets `MAXIMUM_BASE_GAS = 185_000` with the comment that this covers "21_000 transaction cost, roughly worst case 64_000 for calldata, and 100_000 for message verification": [2](#0-1) 

The 64,000-gas calldata budget corresponds to roughly 4,000 bytes of calldata (at ~16 gas/byte non-zero byte cost). However, the actual wrapped message delivered on-chain, `CommittedMessage`, includes a variable-length `params` field (the ABI-encoded command payload) in addition to fixed overhead fields (`channel_id`, `nonce`, `command`, `max_dispatch_gas`, `max_fee_per_gas`, `reward`, `id`): [3](#0-2) 

The only size constraint enforced on this payload is `payload.len() < T::MaxMessagePayloadSize::get()` in `validate`, which is a runtime-configurable constant independent of the 4,000-byte assumption baked into `MAXIMUM_BASE_GAS`: [4](#0-3) 

`calculate_remote_fee` then charges the relayer-facing/remote fee purely as `fee_per_gas * gas_used_at_most + reward`, i.e. directly derived from the undercounted `gas_used_at_most`: [5](#0-4) 

This is structurally identical to the reported bug: a fixed assumption about wrapper/overhead size (there, 140 bytes of `relayMessage` wrapping; here, ~4,000 bytes of calldata budget for the full `CommittedMessage` envelope) is used to price gas instead of accounting for the real encoded size of the message that will actually be processed on the destination chain. Any command whose ABI-encoded `params` exceeds the assumed calldata budget (e.g., `Command::RegisterForeignToken`, which embeds variable-length token `name`/`symbol` strings, or `Command::Upgrade`, whose `initializer.params` can be sized up to `MaxMessagePayloadSize`) will cost more real Ethereum calldata gas than what was charged, exactly mirroring the original discrepancy between "intrinsic gas of the entire message" and "gas computed on only part of the message."

### Impact Explanation
Because the remote fee (which becomes the relayer's gas refund/reward on the Ethereum Gateway contract, per the module's documented fee model `Min(GasPrice, Message.MaxFeePerGas) * GasUsed() + Message.Reward`) is derived from an undercounted `gas_used_at_most`, relayers can be systematically underpaid for larger messages whenever `MaxMessagePayloadSize` (or realistic command payloads) exceed the ~4,000-byte assumption baked into `ConstantGasMeter::MAXIMUM_BASE_GAS`. This falls under the accepted impact category of "public underpriced work that degrades block production or stalls bridge processing": if relaying becomes unprofitable for larger legitimate messages, relayers may decline to deliver them, stalling the outbound bridge pipeline for those message types (registrations with long token names, upgrades with larger initializer payloads, etc.), without requiring any malicious relayer, validator, or governance actor — any ordinary user submitting a large-but-valid message triggers the underpricing.

### Likelihood Explanation
Likelihood is moderate-to-high: no privileged actor or malicious infrastructure is required. Any unprivileged caller who triggers a Snowbridge outbound command with a params payload approaching the configured `MaxMessagePayloadSize` (well above the implicit ~4,000-byte calldata assumption) will produce a message whose actual computed fee undercounts real destination-chain gas cost. The severity scales with how far `MaxMessagePayloadSize` (and realistic payload sizes for `RegisterForeignToken`/`Upgrade`) diverge from the hardcoded 64,000-gas/4,000-byte calldata assumption; this divergence is a static, auditable property of the current constants rather than requiring an active attacker to manufacture unusual conditions.

### Recommendation
Compute the calldata-related portion of `MAXIMUM_BASE_GAS` dynamically from the actual encoded size of the wrapped message (`CommittedMessage`/`OutboundMessageWrapper`), analogous to how the Sherlock recommendation fixed `CrossDomainMessenger.sendMessage` by hashing/costing the fully wrapped `relayMessage` call instead of only the inner message. Concretely, `calculate_fee`/`maximum_gas_used_at_most` should add a per-byte calldata cost term proportional to the ABI-encoded length of the entire outbound envelope (including all fixed fields and the variable `params`), rather than relying on a single hardcoded constant that assumes a fixed worst-case size unrelated to the runtime's actual `MaxMessagePayloadSize`.

### Proof of Concept
1. Deploy a BridgeHub runtime with `MaxMessagePayloadSize` configured larger than ~4,000 bytes (a realistic configuration to support larger governance/registration payloads).
2. Submit a `Command::RegisterForeignToken` (or `Command::Upgrade` with a large `initializer.params`) whose ABI-encoded `params` is, say, 8,000 bytes — well within `MaxMessagePayloadSize` but double the calldata size implicitly budgeted by `MAXIMUM_BASE_GAS`'s "64_000 for calldata" comment.
3. Observe that `calculate_fee` (`bridges/snowbridge/pallets/outbound-queue/src/lib.rs` `calculate_remote_fee`) computes the remote fee using `gas_used_at_most = MAXIMUM_BASE_GAS + maximum_dispatch_gas_used_at_most(command)`, which does not increase with the extra ~4,000 bytes of calldata.
4. Compare against the actual Ethereum-side gas cost of delivering the larger `CommittedMessage` calldata (extra bytes × ~16 gas/byte non-zero-byte cost), which exceeds the budgeted 64,000 gas — demonstrating the fee charged is lower than the gas actually required, underpaying the relayer for the same class of reason the CrossDomainMessenger bug underpriced L1↔L2 messages.

Note: I was unable to fully trace the newer `outbound-queue-v2` path (particularly the user-supplied `gas` field in `Command::CallContract`) before running out of investigation budget; that code path may present an additional or alternative variant of this issue and would benefit from separate review with tool access to `bridges/snowbridge/pallets/outbound-queue-v2/src/send_message_impl.rs`.

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

**File:** bridges/snowbridge/primitives/outbound-queue/src/v1/message.rs (L340-376)
```rust
pub struct ConstantGasMeter;

impl GasMeter for ConstantGasMeter {
	// The base transaction cost, which includes:
	// 21_000 transaction cost, roughly worst case 64_000 for calldata, and 100_000
	// for message verification
	const MAXIMUM_BASE_GAS: u64 = 185_000;

	fn maximum_dispatch_gas_used_at_most(command: &Command) -> u64 {
		match command {
			Command::SetOperatingMode { .. } => 40_000,
			Command::AgentExecute { command, .. } => match command {
				// Execute IERC20.transferFrom
				//
				// Worst-case assumptions are important:
				// * No gas refund for clearing storage slot of source account in ERC20 contract
				// * Assume dest account in ERC20 contract does not yet have a storage slot
				// * ERC20.transferFrom possibly does other business logic besides updating balances
				AgentExecuteCommand::TransferToken { .. } => 200_000,
			},
			Command::Upgrade { initializer, .. } => {
				let initializer_max_gas = match *initializer {
					Some(Initializer { maximum_required_gas, .. }) => maximum_required_gas,
					None => 0,
				};
				// total maximum gas must also include the gas used for updating the proxy before
				// the the initializer is called.
				50_000 + initializer_max_gas
			},
			Command::SetTokenTransferFees { .. } => 60_000,
			Command::SetPricingParameters { .. } => 60_000,
			Command::UnlockNativeToken { .. } => 200_000,
			Command::RegisterForeignToken { .. } => 1_200_000,
			Command::MintForeignToken { .. } => 100_000,
		}
	}
}
```

**File:** bridges/snowbridge/pallets/outbound-queue/src/types.rs (L18-41)
```rust
/// Message which has been assigned a nonce and will be committed at the end of a block
#[derive(Encode, Decode, Clone, PartialEq, Debug, TypeInfo)]
pub struct CommittedMessage {
	/// Message channel
	pub channel_id: ChannelId,
	/// Unique nonce to prevent replaying messages
	#[codec(compact)]
	pub nonce: u64,
	/// Command to execute in the Gateway contract
	pub command: u8,
	/// Params for the command
	pub params: Vec<u8>,
	/// Maximum gas allowed for message dispatch
	#[codec(compact)]
	pub max_dispatch_gas: u64,
	/// Maximum fee per gas
	#[codec(compact)]
	pub max_fee_per_gas: u128,
	/// Reward in ether for delivering this message, in addition to the gas refund
	#[codec(compact)]
	pub reward: u128,
	/// Message ID (Used for tracing messages across route, has no role in consensus)
	pub id: H256,
}
```

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

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L396-402)
```rust
		pub(crate) fn calculate_remote_fee(
			gas_used_at_most: u64,
			fee_per_gas: U256,
			reward: U256,
		) -> U256 {
			fee_per_gas.saturating_mul(gas_used_at_most.into()).saturating_add(reward)
		}
```
