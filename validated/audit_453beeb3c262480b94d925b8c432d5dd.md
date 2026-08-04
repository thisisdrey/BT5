I have sufficient evidence to construct the analog. Here's my finding:

### Title
Hardcoded per-command gas constants in `ConstantGasMeter::maximum_dispatch_gas_used_at_most` can cause permanent loss of bridged ERC-20/ETH funds via out-of-gas failure on Ethereum - ([File: bridges/snowbridge/primitives/outbound-queue/src/v1/message.rs])

### Summary
This mirrors TRST-M-3's core defect: a compiled-in constant (`MAX_SLIPPAGE`) that bounds an operation's acceptable outcome cannot be adjusted when real-world conditions exceed it, causing the operation to permanently fail/revert with no in-protocol remedy. In Snowbridge's outbound queue, `ConstantGasMeter::maximum_dispatch_gas_used_at_most` hardcodes a fixed gas allowance per `Command` (e.g. `UnlockNativeToken { .. } => 200_000`) [1](#0-0) . This value becomes the immutable `max_dispatch_gas` field baked into the `CommittedMessage` once a user's transfer is queued and committed to the outbound Merkle root [2](#0-1) .

### Finding Description
A normal, unprivileged user withdrawing an ERC-20 token from Polkadot back to Ethereum triggers `XcmConverter::make_unlock_native_token_command`, which emits `Command::UnlockNativeToken` [3](#0-2) . When this command is processed by the outbound queue, `T::GasMeter::maximum_dispatch_gas_used_at_most(&queued_message.command)` returns the hardcoded constant, which is written into the `CommittedMessage.max_dispatch_gas` and permanently committed into the block's Merkle root the moment the message is accepted [4](#0-3) . There is no per-call parameter, and no on-chain adjustment mechanism, for this gas allowance — it is compiled into the runtime binary.

The bug-class match is exact: like `MAX_SLIPPAGE`, the constant encodes an assumption about "worst case" cost (analogous to acceptable slippage) that can be violated by real conditions — specific ERC-20 tokens with more expensive `transfer`/`transferFrom` logic (fee-on-transfer, hooks, non-standard implementations) simply need more gas than the hardcoded ceiling. The project's own history confirms this: they had to bump the `TransferToken`/`UnlockNativeToken` gas constant from `100_000` to `200_000` specifically because "LDO token requires 140_000 gas" [5](#0-4) . Any token whose real gas cost exceeds whatever constant is currently hardcoded will cause the Ethereum-side execution of `UnlockNativeToken` to run out of gas and revert.

Unlike TRST-M-3's mitigation, however, this parameter is not admin-adjustable at runtime without a `ConstantGasMeter` code change and full runtime upgrade — there is no equivalent of the `maxSlippage` setter. Once `max_dispatch_gas` is committed to the Merkle root for a given nonce, it cannot be modified: the message content (including gas) is fixed and verified against the committed root by the Ethereum-side light client/verifier.

### Impact Explanation
Because `Command::UnlockNativeToken` releases already-custodied ERC20 tokens/ETH held by the per-agent contract on Ethereum on behalf of a Polkadot-side burn/withdraw that has already been executed and fee-charged, an out-of-gas revert on Ethereum means: (1) the user's asset representation on Polkadot has already been burned/withdrawn and the delivery fee charged, and (2) the actual unlock on Ethereum never succeeds because the transaction reverts, permanently stranding the underlying asset in the agent contract with no automatic retry — this is a permanent user-fund/bridge-state lock, since the message's gas allowance is immutably fixed once committed and cannot be resubmitted with higher gas through the existing nonce-ordered channel.

### Likelihood Explanation
This requires no privileged actor, malicious relayer, or governance action — it can be triggered by any regular user attempting to withdraw a token whose real `transfer`/`transferFrom` gas cost (including possible fee-on-transfer or hook logic) exceeds the hardcoded `ConstantGasMeter` constant for that command. The project has already needed to raise this constant once in response to exactly this failure mode (LDO), demonstrating the class of tokens that can trigger it is not hypothetical.

### Recommendation
Make per-command gas estimates configurable (e.g. via a governance-settable per-token/per-command gas override similar to `PricingParameters`), or require gas headroom validation at registration time for any newly registered foreign/native ERC20 token, so that operators can raise the allowance for specific tokens without requiring a full runtime upgrade and without stranding already-committed messages.

### Proof of Concept
1. Governance registers/permits a native ERC-20 token `X` on the Gateway contract whose `transfer`/`transferFrom` implementation (fee-on-transfer, rebasing, or hook-based) consumes more gas than the hardcoded `UnlockNativeToken` constant (currently `200_000`, previously `100_000`) [6](#0-5) .
2. A user deposits token `X` into Polkadot via the inbound queue, then initiates a reserve-transfer back to Ethereum, producing an XCM program matched by `XcmConverter::make_unlock_native_token_command` [7](#0-6) .
3. `do_process_message` charges the delivery fee, burns the Polkadot-side representation, computes `max_dispatch_gas` from the hardcoded `ConstantGasMeter`, and commits the message (with this fixed gas) into the Merkle root for the assigned nonce [8](#0-7) .
4. A relayer delivers the message and the Gateway contract executes `UnlockNativeToken` with `gas = max_dispatch_gas`; because `X`'s transfer costs more than this ceiling, the Ethereum transaction reverts with out-of-gas.
5. The user's Polkadot-side tokens have already been burned/charged, and the underlying tokens remain locked in the agent contract; no in-protocol mechanism exists to resubmit the same nonce with a higher gas allowance, permanently stranding the funds.

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

**File:** bridges/snowbridge/primitives/outbound-queue/src/v1/converter/mod.rs (L225-317)
```rust
	fn make_unlock_native_token_command(
		&mut self,
	) -> Result<(Command, [u8; 32]), XcmConverterError> {
		use XcmConverterError::*;

		// Get the reserve assets from WithdrawAsset.
		let reserve_assets =
			match_expression!(self.next()?, WithdrawAsset(reserve_assets), reserve_assets)
				.ok_or(WithdrawAssetExpected)?;

		// Check if clear origin exists and skip over it.
		if match_expression!(self.peek(), Ok(ClearOrigin), ()).is_some() {
			let _ = self.next();
		}

		// Get the fee asset item from BuyExecution or continue parsing.
		let fee_asset = match_expression!(self.peek(), Ok(BuyExecution { fees, .. }), fees);
		if fee_asset.is_some() {
			let _ = self.next();
		}

		let (deposit_assets, beneficiary) = match_expression!(
			self.next()?,
			DepositAsset { assets, beneficiary },
			(assets, beneficiary)
		)
		.ok_or(DepositAssetExpected)?;

		// assert that the beneficiary is AccountKey20.
		let recipient = match_expression!(
			beneficiary.unpack(),
			(0, [AccountKey20 { network, key }])
				if self.network_matches(network),
			H160(*key)
		)
		.ok_or(BeneficiaryResolutionFailed)?;

		// Make sure there are reserved assets.
		if reserve_assets.len() == 0 {
			return Err(NoReserveAssets);
		}

		// Check the the deposit asset filter matches what was reserved.
		if reserve_assets.inner().iter().any(|asset| !deposit_assets.matches(asset)) {
			return Err(FilterDoesNotConsumeAllAssets);
		}

		// We only support a single asset at a time.
		ensure!(reserve_assets.len() == 1, TooManyAssets);
		let reserve_asset = reserve_assets.get(0).ok_or(AssetResolutionFailed)?;

		// Fees are collected on AH, up front and directly from the user, to cover the
		// complete cost of the transfer. Any additional fees provided in the XCM program are
		// refunded to the beneficiary. We only validate the fee here if its provided to make sure
		// the XCM program is well formed. Another way to think about this from an XCM perspective
		// would be that the user offered to pay X amount in fees, but we charge 0 of that X amount
		// (no fee) and refund X to the user.
		if let Some(fee_asset) = fee_asset {
			// The fee asset must be the same as the reserve asset.
			if fee_asset.id != reserve_asset.id || fee_asset.fun > reserve_asset.fun {
				return Err(InvalidFeeAsset);
			}
		}

		let (token, amount) = match reserve_asset {
			Asset { id: AssetId(inner_location), fun: Fungible(amount) } => {
				match inner_location.unpack() {
					// Get the ERC20 contract address of the token.
					(0, [AccountKey20 { network, key }]) if self.network_matches(network) => {
						Some((H160(*key), *amount))
					},
					// If there is no ERC20 contract address in the location then signal to the
					// gateway that is a native Ether transfer by using
					// `0x0000000000000000000000000000000000000000` as the token address.
					(0, []) => Some((H160([0; 20]), *amount)),
					_ => None,
				}
			},
			_ => None,
		}
		.ok_or(AssetResolutionFailed)?;

		// transfer amount must be greater than 0.
		ensure!(amount > 0, ZeroAssetTransfer);

		// Check if there is a SetTopic and skip over it if found.
		let topic_id = match_expression!(self.next()?, SetTopic(id), id).ok_or(SetTopicExpected)?;

		Ok((
			Command::UnlockNativeToken { agent_id: self.agent_id, token, recipient, amount },
			*topic_id,
		))
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
