I have sufficient evidence to establish this analog. The `max_dispatch_gas` computed from `ConstantGasMeter`'s hardcoded values is committed into `CommittedMessage` (`bridges/snowbridge/pallets/outbound-queue/src/types.rs`) and sent to the Ethereum Gateway as the gas limit forwarded to command execution, while the same hardcoded number simultaneously determines the fee charged to cover relayer gas refunds via `calculate_fee`/`calculate_remote_fee`.

### Title
Hardcoded per-command gas estimates in `ConstantGasMeter` can underprice and underfund Ethereum-side message dispatch - ([File: bridges/snowbridge/primitives/outbound-queue/src/v1/message.rs])

### Summary
`ConstantGasMeter::maximum_dispatch_gas_used_at_most` hardcodes fixed gas-cost constants (e.g. `40_000`, `50_000`, `60_000`, `100_000`, `200_000`, `1_200_000`) for each `Command` variant dispatched to the Ethereum Gateway contract. These constants are baked into `bridges/snowbridge/primitives/outbound-queue/src/v1/message.rs` (and duplicated in `v2/message.rs`), with no on-chain mechanism to update them if actual EVM gas costs change (EVM repricing forks, e.g. EIP-2929/EIP-1559-style SSTORE/cold-access repricing) or if a specific target contract (e.g. a non-standard ERC20 with expensive `transferFrom`) requires more gas than assumed. This is the same bug class as the reported `SimpleAdministrator.markCost` issue — hardcoded gas figures used for both cost accounting and (here) an actual on-chain execution gas cap, without any setter/update path.

### Finding Description
`GasMeter::maximum_gas_used_at_most` = `MAXIMUM_BASE_GAS` + `maximum_dispatch_gas_used_at_most(command)`, and this value becomes `max_dispatch_gas` on the `CommittedMessage` struct that is ABI-encoded and delivered to the Ethereum Gateway contract [1](#0-0) . The same hardcoded figure is used to compute `RemoteFee = MaxGasRequired(Message) * Params.MaxFeePerGas + Params.Reward`, per the module documentation [2](#0-1) .

The gas figures themselves are hardcoded constants derived from a point-in-time `forge test --gas-report` snapshot, with only a "healthy buffer" as margin, not a governance-configurable parameter: [3](#0-2) 

This class of bug has already manifested in production: a prior fix bumped the `TransferToken` gas limit from `100_000` to `200_000` after discovering that the LDO ERC20 token requires `140_000` gas, exceeding the hardcoded assumption [4](#0-3) . Unlike `SimpleAdministrator.markCost`, which is purely off-chain accounting, this hardcoded gas figure is used on-chain by the Gateway contract as the actual gas forwarded/capped for the command's execution. If any target call underestimated by this constant runs out of gas, execution reverts on Ethereum, yet the Substrate side has already: (1) incremented the per-channel `Nonce` and (2) permanently committed the message into the Merkle root via `do_process_message`/`commit`, with no retry or resend path [5](#0-4) .

### Impact Explanation
Because the nonce and commitment are irreversible once queued, an underestimated hardcoded gas figure (from unrelated ERC20 logic complexity, future EVM repricing, or a newly registered foreign token with more complex transfer logic) causes the corresponding `UnlockNativeToken`/`MintForeignToken`/`RegisterForeignToken` command to permanently fail on Ethereum without any on-chain path to resubmit it with more gas — a permanent bridge-state/fund lock for the affected message, and simultaneously the relayer's fee/reward calculation (`calculate_remote_fee`) is based on the same underestimated gas, degrading the guarantee that fees paid actually cover the real dispatch cost.

### Likelihood Explanation
Medium: this exact scenario already occurred once in this codebase (the LDO/`TransferToken` 100k→200k gas fix), showing the hardcoded assumptions are fragile against real-world contract behavior and will recur whenever a new foreign token, target contract, or EVM gas repricing event exceeds the fixed constant.

### Recommendation
Make per-command gas allowances a runtime-configurable/governance-updatable parameter (similar to `PricingParameters`, which is already updatable via `set_pricing_parameters`) rather than compile-time constants in `ConstantGasMeter`, and/or add a safety margin plus an on-chain retry/resend mechanism for messages that fail on Ethereum due to insufficient forwarded gas.

### Proof of Concept
1. A new `RegisterForeignToken`/`MintForeignToken`/`UnlockNativeToken` command targets a token/contract whose actual gas requirement exceeds the hardcoded constant in `ConstantGasMeter` (as already happened with LDO exceeding the `100_000` `TransferToken` assumption, per `prdoc/stable2503-1/pr_7947.prdoc`).
2. `do_process_message` computes `max_dispatch_gas` from this constant, increments `Nonce`, and commits the message into the Merkle root — this step is final and cannot be undone [6](#0-5) .
3. On Ethereum, the Gateway contract executes the command capped at `max_dispatch_gas`; execution reverts due to out-of-gas.
4. The message is now permanently stuck: its nonce was consumed, it was already committed to the Merkle root, and there is no code path to regenerate/resend it with a higher gas allowance, resulting in permanent loss of the intended state transition (e.g., locked tokens never unlocked on Ethereum).

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L60-67)
```rust
//! ## Fee Computation Function
//!
//! ```text
//! LocalFee(Message) = WeightToFee(ProcessMessageWeight(Message))
//! RemoteFee(Message) = MaxGasRequired(Message) * Params.MaxFeePerGas + Params.Reward
//! RemoteFeeAdjusted(Message) = Params.Multiplier * (RemoteFee(Message) / Params.Ratio("ETH/DOT"))
//! Fee(Message) = LocalFee(Message) + RemoteFeeAdjusted(Message)
//! ```
```

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L300-363)
```rust
		/// Process a message delivered by the MessageQueue pallet
		pub(crate) fn do_process_message(
			_: ProcessMessageOriginOf<T>,
			mut message: &[u8],
		) -> Result<bool, ProcessMessageError> {
			use ProcessMessageError::*;

			// Yield if the maximum number of messages has been processed this block.
			// This ensures that the weight of `on_finalize` has a known maximum bound.
			ensure!(
				MessageLeaves::<T>::decode_len().unwrap_or(0) <
					T::MaxMessagesPerBlock::get() as usize,
				Yield
			);

			// Decode bytes into versioned message
			let versioned_queued_message: VersionedQueuedMessage =
				VersionedQueuedMessage::decode(&mut message).map_err(|_| Corrupt)?;

			// Convert versioned message into latest supported message version
			let queued_message: QueuedMessage =
				versioned_queued_message.try_into().map_err(|_| Unsupported)?;

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

			Self::deposit_event(Event::MessageAccepted { id: queued_message.id, nonce });

			Ok(true)
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

**File:** prdoc/stable2503-1/pr_7947.prdoc (L1-8)
```text
title: Snowbridge - Update TransferToken command gas limit.

doc:
  - audience: Runtime Dev
    description: |
      Transfering certain ERC20 tokens require more gas than 100_000 gas. An example is LDO token which requires 140_000 gas.
      This change updates the gas limit to 200_000 and also updates the default fees for testnet runtimes.
      NOTE: make sure to update the relevant runtime fees to account for this change.
```
