## Analysis

The GMX bug's core pattern — **a hardcoded gas value baked into a bridge message that is smaller than what execution on the receiving side actually requires, causing the dispatched call to revert while the sending side has already irreversibly moved funds** — has a direct structural analog in Snowbridge's outbound queue gas metering. [1](#0-0) 

### Title
Hardcoded `ConstantGasMeter` dispatch-gas values can be insufficient for Ethereum-side execution, causing permanent loss of bridged funds - ([File: bridges/snowbridge/primitives/outbound-queue/src/v1/message.rs])

### Summary
`ConstantGasMeter::maximum_dispatch_gas_used_at_most` assigns fixed, hardcoded gas budgets per `Command` variant (e.g. `UnlockNativeToken => 200_000`, `MintForeignToken => 100_000`, `RegisterForeignToken => 1_200_000`) that are embedded as `max_dispatch_gas`/`gas` in the `CommittedMessage`/`OutboundCommandWrapper` sent to the Ethereum Gateway contract. These figures are static estimates ("extracted from a forge gas-report"), not derived from the actual runtime behavior of arbitrary ERC-20 tokens or agent execution targets. This is functionally identical to Vaultka's hardcoded `2_000_000` `callbackGasLimit` versus GMX's configurable, potentially-lower `MAX_CALLBACK_GAS_LIMIT` — a static gas assumption baked into a cross-domain message that later gets validated/consumed by an external execution environment whose real gas cost can exceed it.

### Finding Description
When a user bridges a native token off Ethereum via XCM (`WithdrawAsset` → `DepositAsset` on AssetHub), the converter builds `Command::UnlockNativeToken` [2](#0-1)  and the outbound queue pallet computes `max_dispatch_gas` purely from the hardcoded table in `ConstantGasMeter`, not from any property of the specific ERC20 token being unlocked: [3](#0-2) . This value is committed into the message that is later relayed and verified on Ethereum, where the Gateway contract uses it as the gas stipend for the low-level dispatch call to the token/agent execution path.

The bridge team has already had to patch this exact class of problem once in production: [4](#0-3)  documents that the LDO ERC20 token required 140_000 gas against a then-hardcoded 100_000 limit, causing reverts. The fix only bumped the constant to 200_000 for that one observed case — it does not make the value configurable or dynamically computed, so any other ERC20 with unusual `transferFrom`/hook logic (fee-on-transfer, rebasing, gas-heavy `Transfer` event listeners, proxy-based tokens) that needs more than the hardcoded figure will hit the same failure mode again, and there is no governance-independent way to raise the limit per-token from the runtime side.

Unlike the pallet-contracts/pallet-revive gas meters found in this repo — which are dynamic, caller-supplied, and enforced against the actual local execution engine — this Snowbridge value is a compile-time constant asserted about the behavior of an *external, non-Substrate execution environment* (the Ethereum Gateway/token contracts), which the runtime cannot control and cannot verify ahead of time.

### Impact Explanation
By the time the message is committed and the merkle root is inserted into the header digest, the `WithdrawAsset` on the Substrate side has already executed and the corresponding tokens are gone from the user's local balance (moved to the bridge's escrow/agent state), per the XCM flow feeding `make_unlock_native_token_command`. If the Ethereum Gateway's dispatch call reverts due to insufficient hardcoded gas, the nonce is still consumed as "delivered" from the relayer's perspective (delivery proof still verifies since it only proves inclusion/execution attempt, not dispatch success), but the actual unlock/mint on Ethereum never happens. This results in a permanent, unrecoverable loss of the user's bridged funds — squarely matching the "permanent user-fund or bridge-state lock" impact category, without requiring any malicious relayer, validator, or governance actor: it is triggered purely by using an ERC20 token whose gas cost exceeds the hardcoded table, which is public, unprivileged behavior (any user can select which ERC20 to bridge).

### Likelihood Explanation
Likelihood is non-trivial and has concrete precedent: the LDO-token incident in `pr_7947` shows the hardcoded assumption has already been proven wrong once for `UnlockNativeToken`/`TransferToken` dispatch, and the fix was reactive (raising one constant) rather than structural. Since `UnlockNativeToken`/`MintForeignToken`/`RegisterForeignToken` gas figures remain flat per-command regardless of which specific token/target is involved, any newly-listed ERC20 with heavier `transfer`/hook logic than assumed can reproduce the same failure, and this is entirely dependent on public token behavior outside protocol control.

### Recommendation
Make the dispatch gas limit for `UnlockNativeToken`/`MintForeignToken`/`RegisterForeignToken`/`AgentExecute` configurable per-token (or at minimum governance-adjustable at runtime, similar to how `Command::CallContract` in v2 already takes an explicit caller-supplied `gas` parameter — [5](#0-4) ), rather than relying solely on a static `ConstantGasMeter` table shared across all tokens of a given command kind. Consider tracking a per-token registered gas requirement (populated at token-registration time and adjustable via governance) so unusually gas-hungry tokens don't silently exceed the hardcoded assumption.

### Proof of Concept
1. A foreign ERC20 token with non-standard `transfer`/`transferFrom` logic (e.g. fee-on-transfer, rebasing accounting, or additional hook calls) is registered/bridged such that its real gas cost during dispatch on Ethereum exceeds 200_000 gas (the current `UnlockNativeToken` constant in `ConstantGasMeter`).
2. A user submits an XCM `WithdrawAsset`+`DepositAsset` for this token on AssetHub; `make_unlock_native_token_command` builds `Command::UnlockNativeToken`, and the tokens are withdrawn from the user's local balance immediately as part of XCM execution.
3. `do_process_message` computes `max_dispatch_gas` from the hardcoded 200_000 constant [6](#0-5)  and commits the message.
4. On Ethereum, the Gateway contract dispatches the unlock call with the committed gas limit; the call runs out of gas and reverts because the token's real cost exceeds 200_000.
5. The relayer's delivery proof (of inclusion/attempted dispatch) is still accepted, so the nonce is consumed, but the recipient never receives the unlocked tokens — the user's funds are permanently lost, exactly mirroring the GMX/Vaultka scenario where a hardcoded gas figure mismatched with the actual downstream execution cost caused unconditional reverts of legitimate operations.

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

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L332-352)
```rust
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
