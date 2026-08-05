### Title
Hardcoded per-command gas limits in Snowbridge `ConstantGasMeter` allow payload-size-dependent execution to exceed the reserved Ethereum gas, causing on-chain governance/asset commands to run out of gas and be permanently lost - (File: bridges/snowbridge/primitives/outbound-queue/src/v1/message.rs, bridges/snowbridge/primitives/outbound-queue/src/v2/message.rs)

### Summary
Both Snowbridge outbound-queue message primitives (v1 and v2) compute the gas reserved for executing a command on the Ethereum Gateway contract using a `ConstantGasMeter` that assigns a **fixed, hardcoded gas value per command variant**, independent of the actual size/content of the command's payload. This is a direct local analog of the reported bug class: cross-chain messages are priced/gassed using static constants rather than payload-aware estimation, so messages whose execution cost scales with input size (variable-length lists, foreign-token metadata, upgrade initializer calldata) can exceed the gas reserved for them and revert/fail on Ethereum, permanently stranding governance or asset operations dispatched through the bridge.

### Finding Description
`GasMeter::maximum_dispatch_gas_used_at_most` in `ConstantGasMeter` (v1) returns fixed numbers per `Command` variant, e.g.: [1](#0-0) 

Notably:
- `Command::RegisterForeignToken` is charged a flat `1_200_000` gas regardless of the actual token metadata (name/symbol/decimals) encoded in the command, even though registering a new ERC-20-like foreign asset on the Gateway involves deploying/initializing token-specific state whose cost is influenced by the encoded strings.
- `Command::Upgrade` computes gas as `50_000 + initializer_max_gas`, where `initializer_max_gas` comes from the `Initializer.maximum_required_gas` field supplied by the *message constructor* itself (governance), not derived from measuring the actual `initializer.params` payload size, so the gas reservation can silently diverge from real EVM execution cost if the initializer logic scales with `params` length.
- `Command::AgentExecute`/`TransferToken` and `UnlockNativeToken` are charged `200_000` flat, independent of amounts, destination contract complexity, or the number of downstream calls the destination token contract performs (e.g., non-standard/complex ERC-20 hooks).

The v2 primitives repeat the same design: [2](#0-1) 

The only place where the gas is fully caller-supplied (not hardcoded) is `Command::CallContract { gas: gas_limit, .. } => *gas_limit`, which shows the codebase authors implicitly acknowledge that gas should be measured/estimated per call — but they didn't apply the same principle to `RegisterForeignToken`, `Upgrade`'s base cost, or `UnlockNativeToken`/`AgentExecute`/`MintForeignToken`.

This `maximum_dispatch_gas_used_at_most` value is used both for:
1. Fee calculation (`calculate_fee`) so the sender is charged based on the assumed gas.
2. The `gas` field embedded into the committed/ABI-encoded message that is verified and executed on Ethereum: [3](#0-2) [4](#0-3) 

Because the gas figure that is committed on-chain and relied upon by the Ethereum Gateway is a static constant per command type rather than a function of the actual encoded payload size/complexity, any Polkadot-side command whose real Ethereum execution cost depends on variable-length data (token metadata strings in `RegisterForeignToken`, or list-like initializer parameters in `Upgrade`) can be under-gassed. When the Ethereum Gateway executes the command with `gas <= committed_gas` and it runs out of gas mid-execution, the command permanently fails and the corresponding nonce/order is marked processed (there is no retry path — nonces increment unconditionally on the Polkadot side once queued, and the delivery receipt / pending order flow settles the relayer reward independent of whether the *command* itself succeeded on Ethereum, only that delivery occurred).

### Impact Explanation
An under-gassed `RegisterForeignToken` or `Upgrade` command will revert on the Ethereum Gateway due to out-of-gas, while the Polkadot side has already incremented the nonce, appended the message to `Messages`/`MessageLeaves`, and (in v2) created a `PendingOrder` that pays the relayer once delivery is confirmed — regardless of the *command's* success. This means:
- A governance-initiated `Upgrade` or asset registration can be silently and permanently lost (never re-sent, since nonces are monotonic and the message is already committed/merkle-proved).
- This is a "permanent bridge-state lock" / stalled bridge processing scenario matching the pivot criteria (message queues/bridge markers advance without atomic guarantee that execution succeeded), landing in Medium-High impact territory since it can affect governance/asset bridge operations, not user funds directly in every case, but can freeze token registration or contract upgrades indefinitely.

### Likelihood Explanation
Likelihood is Medium: the vulnerable paths (`RegisterForeignToken`, `Upgrade`) are typically triggered by governance/system pallets rather than arbitrary end users, so exploitation requires a legitimate registration/upgrade action with unusually large metadata or initializer payload — not an attacker-controlled trigger by itself. However, this is a functional correctness bug (not requiring any malicious actor), consistent with the external report's root cause: any team/registrar submitting a slightly larger-than-assumed payload (e.g., a long token name/symbol via `register_token` or a custom initializer) can trigger the failure without any adversarial behavior.

### Recommendation
Replace static per-command gas constants with a gas estimation function that accounts for payload size (e.g., scale `RegisterForeignToken` gas with the length of encoded token metadata, and validate/measure `Initializer.params` length against `maximum_required_gas` rather than trusting the caller-supplied value). Add a safety margin proportional to payload length (similar to how `receive_messages_proof_weight` already accounts for `EXPECTED_DEFAULT_MESSAGE_LENGTH` and `storage_proof_size_overhead` on the inbound side) instead of a single constant regardless of message content. Additionally, ensure the Polkadot-side nonce/committed-message flow does not treat delivery as final success independent of the destination contract's actual execution outcome, or provide a governance-triggered replay/resend mechanism for commands that revert due to out-of-gas on Ethereum.

### Proof of Concept
1. Governance calls `register_token` (or the runtime API path that builds `Command::RegisterForeignToken`) for a foreign asset with an unusually long/verbose metadata payload (name/symbol strings near the practical encoding limit). [5](#0-4) 
2. `ConstantGasMeter::maximum_dispatch_gas_used_at_most` returns the flat `1_200_000` regardless of payload size.
3. `do_process_message` embeds this constant gas into the committed message and advances `Nonce`/`MessageLeaves` unconditionally: [6](#0-5) 
4. The relayer delivers the message to Ethereum; if actual gas needed by `RegisterForeignToken` execution (proportional to the ABI-encoded metadata length) exceeds `1_200_000`, the Gateway call reverts/out-of-gas.
5. Because the nonce was already consumed and the message already merkle-committed on the Polkadot side, there is no built-in mechanism to resend — the token registration/upgrade is permanently lost, matching the "loss of cross-chain deployment" impact from the external report, transposed to Snowbridge's outbound governance/asset command flow.

### Citations

**File:** bridges/snowbridge/primitives/outbound-queue/src/v1/message.rs (L342-376)
```rust
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

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/message.rs (L281-306)
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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L371-379)
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
```

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L323-359)
```rust
			// Obtain next nonce
			let nonce = <Nonce<T>>::try_mutate(
				queued_message.channel_id,
				|nonce| -> Result<u64, ProcessMessageError> {
					*nonce = nonce.checked_add(1).ok_or(Unsupported)?;
					Ok(*nonce)
				},
			)?;

			let pricing_params = T::PricingParameters::get();
			let command = queued_message.command.index();
			let params = queued_message.command.abi_encode();
			let max_dispatch_gas =
				T::GasMeter::maximum_dispatch_gas_used_at_most(&queued_message.command);
			let reward = pricing_params.rewards.remote;

			// Construct the final committed message
			let message = CommittedMessage {
				channel_id: queued_message.channel_id,
				nonce,
				command,
				params,
				max_dispatch_gas,
				max_fee_per_gas: pricing_params
					.fee_per_gas
					.try_into()
					.defensive_unwrap_or(u128::MAX),
				reward: reward.try_into().defensive_unwrap_or(u128::MAX),
				id: queued_message.id,
			};

			// ABI-encode and hash the prepared message
			let message_abi_encoded = ethabi::encode(&[message.clone().into()]);
			let message_abi_encoded_hash = <T as Config>::Hashing::hash(&message_abi_encoded);

			Messages::<T>::append(Box::new(message));
			MessageLeaves::<T>::append(message_abi_encoded_hash);
```
