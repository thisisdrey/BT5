### Title
Hardcoded per-command gas ceilings in `ConstantGasMeter` can under-price Ethereum dispatch, causing permanent, non-retryable Snowbridge message failure - ([File: bridges/snowbridge/primitives/outbound-queue/src/v1/message.rs])

### Summary
The Snowbridge outbound queue prices and caps the gas that a message is allowed to consume on the Ethereum Gateway contract using hardcoded, compile-time constants in `ConstantGasMeter::maximum_dispatch_gas_used_at_most`, e.g. `TransferToken { .. } => 200_000`, `UnlockNativeToken { .. } => 200_000`, `RegisterForeignToken { .. } => 1_200_000`, `MintForeignToken { .. } => 100_000` [1](#0-0) . This is structurally the same bug class as the in3-server report: a hardcoded gas figure is used both to price a fee and to bound execution of an external-chain transaction, with no dynamic re-estimation, so if real EVM execution costs exceed the hardcoded constant (e.g. ERC20 `transferFrom` with additional logic, gas-cost-increasing EVM upgrades, or a newly-listed foreign token with unusual storage patterns), the on-chain Ethereum dispatch runs out of gas and the message can never succeed — while the Substrate side has already consumed the nonce, collected the fee, and committed the message irreversibly.

### Finding Description
`do_process_message` in the outbound-queue pallet assigns a strictly increasing nonce, computes `max_dispatch_gas` from `T::GasMeter::maximum_dispatch_gas_used_at_most`, and irrevocably commits the message (ABI-encoded, hashed into the Merkle leaves for the block's digest) before any actual Ethereum execution takes place [2](#0-1) . The fee charged to the user is likewise computed upfront from this same hardcoded gas ceiling via `calculate_fee`/`calculate_remote_fee` in `validate()` [3](#0-2) [4](#0-3) .

The gas figures themselves are static Rust constants derived from a point-in-time `forge test --gas-report` snapshot with a "healthy buffer," not from any live estimation mechanism such as `eth_estimateGas` [5](#0-4) . Unlike pricing parameters (`fee_per_gas`, `reward`, `multiplier`, `exchange_rate`) which are explicitly designed to be updated periodically via `set_pricing_parameters`, the per-command gas ceilings are compiled into the runtime and require a runtime upgrade to change. This is exactly the failure mode the external report calls out: hardcoded gas limits that don't track network/execution-cost reality.

There is prior evidence in-repo that this exact constant has already needed correction once — PR `pr_7947`/`pr_8259` raised the `TransferToken` gas limit from 100,000 to 200,000 after discovering that some ERC20 tokens (e.g. LDO) require more gas than assumed [6](#0-5) . This demonstrates the constant is a moving target dependent on external token behavior that the pallet cannot observe, and other commands (`RegisterForeignToken`, `UnlockNativeToken`, `MintForeignToken`, `AgentExecute`) are equally exposed with no compensating mechanism.

### Impact Explanation
Once `do_process_message` executes, the nonce is consumed, the fee has been deducted, and the message is Merkle-committed into the block header digest — this state transition is irreversible from the Substrate side. If the embedded `max_dispatch_gas` proves insufficient for a given command's actual on-chain execution (a newly onboarded ERC20 token with expensive `transferFrom` logic, an EVM gas-repricing hard fork, or an unusually large registration payload), the message permanently fails on Ethereum: it cannot be resubmitted with a higher gas limit because `max_dispatch_gas` is baked into the immutable committed message and enforced by the Gateway contract. This can permanently lock user funds in transit (e.g. a `TransferToken`/`UnlockNativeToken` command that can never execute) or stall governance/system operations that rely on the same queue, degrading bridge processing exactly as the impact gate describes ("public underpriced work that degrades block production or stalls bridge processing" / "permanent user-fund or bridge-state lock").

### Likelihood Explanation
No malicious actor, governance abuse, or privileged access is required — this is triggered purely by mismatch between a static, pallet-internal constant and real-world EVM gas costs, which is data any ordinary user's cross-chain transfer can encounter (as already happened once with the LDO token). The likelihood is directly tied to token/contract diversity growing over time and to Ethereum gas-cost changes (EIPs), both of which are outside the control of the bridge and not detected by any on-chain safeguard before commitment.

### Recommendation
Do not hardcode per-command Ethereum gas ceilings as immutable Rust constants. At minimum:
- Make the gas ceilings for each `Command` variant governance-adjustable parameters (similar to `PricingParameters`), so they can be raised without a runtime upgrade when a new/expensive token or command is registered.
- Where possible (e.g. for `RegisterForeignToken`/token-specific commands), require or allow submission of a per-transaction gas estimate/override, analogous to using `eth_estimateGas` with `max(HARDCODED_GAS, estimated_amount)`, validated against a sane upper bound before commitment.
- Add a safety margin check/alerting when actual relayer-reported gas usage (via `submit_delivery_receipt`) approaches the hardcoded ceiling for a command, to proactively flag commands whose constants need updating before failures occur.

### Proof of Concept
1. A user submits a `TransferToken`/`UnlockNativeToken` message for an ERC20 token whose `transferFrom` (or equivalent) implementation consumes materially more gas than the current constant (as already happened with LDO, requiring the constant to be bumped from 100,000 to 200,000 in `pr_7947`) [6](#0-5) .
2. `validate()` computes the fee from `GasMeter::maximum_gas_used_at_most`, and the message is enqueued [3](#0-2) .
3. `do_process_message` assigns the next nonce, embeds `max_dispatch_gas` from the same hardcoded constant, and irreversibly commits the message into `Messages`/`MessageLeaves` for the block's Merkle root [2](#0-1) .
4. A relayer submits the message to the Ethereum Gateway contract with the embedded `max_dispatch_gas`; execution runs out of gas and reverts.
5. Because the nonce/commitment is already finalized and immutable, there is no path within this codebase to resubmit the same logical operation with a higher gas allowance — the transfer/unlock is permanently stuck, and the fee already collected does not fund a successful retry.

### Citations

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

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L300-364)
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
		}
```

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L366-402)
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

		/// Calculate fee in remote currency for dispatching a message on Ethereum
		pub(crate) fn calculate_remote_fee(
			gas_used_at_most: u64,
			fee_per_gas: U256,
			reward: U256,
		) -> U256 {
			fee_per_gas.saturating_mul(gas_used_at_most.into()).saturating_add(reward)
		}
```

**File:** bridges/snowbridge/pallets/outbound-queue/src/send_message_impl.rs (L41-74)
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

		// Ensure there is a registered channel we can transmit this message on
		ensure!(T::Channels::contains(&message.channel_id), SendError::InvalidChannel);

		// Generate a unique message id unless one is provided
		let message_id: H256 = message
			.id
			.unwrap_or_else(|| unique((message.channel_id, &message.command)).into());

		let gas_used_at_most = T::GasMeter::maximum_gas_used_at_most(&message.command);
		let fee = Self::calculate_fee(gas_used_at_most, T::PricingParameters::get());

		let queued_message: VersionedQueuedMessage = QueuedMessage {
			id: message_id,
			channel_id: message.channel_id,
			command: message.command.clone(),
		}
		.into();
		// The whole message should not be too large
		let encoded = queued_message.encode().try_into().map_err(|_| SendError::MessageTooLarge)?;

		let ticket = Ticket { message_id, channel_id: message.channel_id, message: encoded };

		Ok((ticket, fee))
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
