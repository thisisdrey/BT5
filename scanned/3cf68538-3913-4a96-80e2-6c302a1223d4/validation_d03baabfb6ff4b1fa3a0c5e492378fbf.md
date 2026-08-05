### Title
Constant-gas Ethereum dispatch metering in Snowbridge outbound queue can under-price/under-fund cross-chain message execution, causing stuck/undeliverable commands and permanent lock of bridged funds or governance actions - ([File: bridges/snowbridge/primitives/outbound-queue/src/v2/message.rs])

### Summary
The external report's core invariant is: cross-chain messages must be priced/funded with gas estimates that actually match execution cost on the destination chain; hardcoded gas figures break this invariant when the real cost varies with payload/target. The same pattern exists natively in Snowbridge's outbound queue, where `ConstantGasMeter::maximum_dispatch_gas_used_at_most` [1](#0-0)  assigns fixed, hardcoded gas values per `Command` variant (e.g. `UnlockNativeToken` = 200_000, `MintForeignToken` = 100_000, `RegisterForeignToken` = 1_200_000) rather than deriving the gas requirement from the actual payload (token contract behavior, list/array lengths, etc.). The identical pattern exists in v1 (`ConstantGasMeter` in `bridges/snowbridge/primitives/outbound-queue/src/v1/message.rs`) [2](#0-1) .

### Finding Description
`GasMeter::maximum_dispatch_gas_used_at_most` is the mechanism used to bound the gas that will be provided to the Gateway contract on Ethereum when a command is dispatched [3](#0-2) . For most command kinds this value is a hardcoded constant, independent of the actual on-chain token/contract behavior that will be invoked (e.g., `TransferToken`/`UnlockNativeToken` calls arbitrary ERC20 `transferFrom`/`transfer` implementations, whose gas cost is not knowable statically) [4](#0-3) .

This is not theoretical: the repository itself documents that the hardcoded 100_000 gas figure for `TransferToken`/token-unlock commands was insufficient for real tokens (LDO required 140_000 gas), and had to be bumped to 200_000 via a runtime upgrade, as recorded in the prdocs `pr_7947.prdoc` and `pr_8259.prdoc` [5](#0-4) [6](#0-5) . This confirms the class of bug from the report (fixed gas insufficient for destination-chain execution) is a real, previously-manifested issue in this exact subsystem, not merely a hypothetical analog.

The v2 primitives additionally introduce `Command::CallContract { gas: gas_limit, .. }`, where the gas limit is taken directly from a caller/relayer-supplied field with no lower/upper bound validation shown in `ConstantGasMeter` beyond passing it straight through: `Command::CallContract { gas: gas_limit, .. } => *gas_limit` [7](#0-6) . If this figure is used both to size the fee charged on the source chain and to bound the gas forwarded to the target contract on Ethereum, any mismatch between the declared gas and what is actually required (whether hardcoded and too low, or attacker/user supplied and unchecked) can cause the Ethereum-side dispatch to run out of gas mid-execution.

### Impact Explanation
When an outbound message's ABI-encoded gas figure is insufficient for the real execution cost on Ethereum, the destination-side `Gateway` contract call reverts or partially executes (out-of-gas), while the Polkadot side has already treated the message as sent/committed (it is merkle-committed and queued as `MessageAccepted`/`MessagesCommitted`) [8](#0-7) . This mirrors the report's "loss of cross-chain DAO deployment" pattern applied to Snowbridge: commands like `UnlockNativeToken`, `MintForeignToken`, or `RegisterForeignToken` can fail on Ethereum after being irreversibly consumed on the Polkadot side, resulting in funds that are locked/unlockable-but-never-unlocked, minting operations that never complete, or governance/upgrade commands that silently fail — all without any retry/estimation mechanism baked into the gas meter itself.

### Likelihood Explanation
Likelihood is Medium: this doesn't require a malicious actor — it is triggered by legitimate use of tokens/contracts whose gas cost exceeds the hardcoded assumption (as already proven historically by the LDO token case that forced a governance-driven constant bump). Any newly-listed foreign token or governance command whose Ethereum-side gas cost exceeds the current hardcoded constant reproduces the same failure mode until developers notice and manually raise the constant again.

### Recommendation
Replace fixed per-command constants in `ConstantGasMeter` with a gas estimation mechanism that accounts for payload size, target-specific execution characteristics (e.g., token contract behavior), and a safety margin, similar to the `baseGas`/`intrinsicGas`/`executionGas` breakdown recommended in the original report. Where a caller-supplied gas (`CallContract`) is used, enforce sane minimum/maximum bounds and cross-check against the fee actually charged so a mismatch cannot cause an irreversible commit against an underfunded execution.

### Proof of Concept
1. A user or pallet issues an `UnlockNativeToken`/`MintForeignToken` command for an ERC20 whose `transfer`/`mint` hook consumes more gas than the hardcoded constant (200,000 / 100,000 respectively) — as already happened with the LDO token requiring 140,000 gas against the old 100,000 constant [5](#0-4) .
2. `Pallet::validate`/`deliver` on Polkadot accepts and commits the message using `ConstantGasMeter::maximum_dispatch_gas_used_at_most` to size the fee [1](#0-0) , and the message is merkle-committed as delivered/accepted [9](#0-8) .
3. On Ethereum, the Gateway contract executes the command with the constant gas limit; because actual execution cost exceeds it, the call reverts/out-of-gas.
4. The Polkadot-side state (nonce advanced, fee spent, message committed) cannot be rolled back, so the cross-chain operation (unlock/mint/token registration) is permanently lost from the user's perspective, matching the "loss of cross-chain deployment" impact in the original report.

### Citations

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/message.rs (L275-279)
```rust
pub trait GasMeter {
	/// Measures the maximum amount of gas a command payload will require to *dispatch*, NOT
	/// including validation & verification.
	fn maximum_dispatch_gas_used_at_most(command: &Command) -> u64;
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

**File:** bridges/snowbridge/primitives/outbound-queue/src/v1/message.rs (L332-376)
```rust
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

**File:** prdoc/stable2503-1/pr_7947.prdoc (L1-9)
```text
title: Snowbridge - Update TransferToken command gas limit.

doc:
  - audience: Runtime Dev
    description: |
      Transfering certain ERC20 tokens require more gas than 100_000 gas. An example is LDO token which requires 140_000 gas.
      This change updates the gas limit to 200_000 and also updates the default fees for testnet runtimes.
      NOTE: make sure to update the relevant runtime fees to account for this change.

```

**File:** prdoc/stable2503-1/pr_8259.prdoc (L1-9)
```text
title: Snowbridge - Update TransferToken command gas limit.

doc:
  - audience: Runtime Dev
    description: |
      Transfering certain ERC20 tokens require more gas than 100_000 gas. An example is LDO token which requires 140_000 gas.
      This change updates the gas limit to 200_000 and also updates the default fees for testnet runtimes.
      NOTE: make sure to update the relevant runtime fees to account for this change.

```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L177-223)
```rust
	#[pallet::event]
	#[pallet::generate_deposit(pub fn deposit_event)]
	pub enum Event<T: Config> {
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
			/// cannot be decoded.
			payload: Vec<u8>,
			/// The error that was returned.
			error: ProcessMessageError,
		},
		/// Message was not committed due to being overweight or the current block is full.
		MessagePostponed {
			/// The payload of the message. Useful for debugging purposes if the message
			/// cannot be decoded.
			payload: Vec<u8>,
			/// The error that was returned.
			reason: ProcessMessageError,
		},
		/// Some messages have been committed
		MessagesCommitted {
			/// Merkle root of the committed messages
			root: H256,
			/// number of committed messages
			count: u64,
		},
		/// Set OperatingMode
		OperatingModeChanged { mode: BasicOperatingMode },
		/// Delivery Proof received
		MessageDelivered { nonce: u64 },
	}
```
