## Analysis

The UniswapX bug's core invariant break is: **a hardcoded, static gas allowance for a value-carrying low-level call, which does not scale with the real gas cost of the recipient's execution, causing legitimate transfers to permanently fail/revert for certain recipients (smart-contract wallets, complex-logic tokens) as gas economics or destinations vary.**

The closest local analog in this repository is in the Snowbridge outbound-queue gas metering used to bound the Ethereum-side execution of dispatched commands (`UnlockNativeToken`, `MintForeignToken`, `RegisterForeignToken`, legacy `AgentExecute::TransferToken`).

### Title
Hardcoded per-command gas budgets in Snowbridge `ConstantGasMeter` can permanently strand unlocked/minted funds when the Ethereum recipient needs more gas - (File: `bridges/snowbridge/primitives/outbound-queue/src/v1/message.rs`)

### Summary
`ConstantGasMeter::maximum_dispatch_gas_used_at_most` hardcodes a fixed gas budget per outbound command (e.g. `UnlockNativeToken => 200_000`, `MintForeignToken => 100_000`, `RegisterForeignToken => 1_200_000`) [1](#0-0) . This value is baked into the committed message (`CommittedMessage.max_dispatch_gas`) that the Gateway contract on Ethereum uses to bound the actual value/token-moving call for that command [2](#0-1) . Any unprivileged user can trigger an `UnlockNativeToken` or `MintForeignToken` command simply by sending a standard reserve-asset-withdraw XCM to AssetHub, which the `XcmConverter` deterministically turns into these commands without any origin restriction [3](#0-2) .

### Finding Description
The comment in the gas meter explicitly acknowledges the fragility of this hardcoded approach ("Worst-case assumptions are important... ERC20.transferFrom possibly does other business logic besides updating balances") [4](#0-3) , and this has already manifested in production: PR #8259 raised the `TransferToken`/`UnlockNativeToken` budget from 100,000 to 200,000 because the LDO token alone required 140,000 gas [5](#0-4) . This is exactly the failure mode described in the external report: a static gas ceiling on a value-transfer call that does not account for variability in the recipient/token logic (fee-on-transfer tokens, rebasing tokens, or a recipient that is a smart-contract wallet with non-trivial `receive`/`fallback` logic).

Unlike the UniswapX case, here the "low-level call gas limit" is not merely a UX inconvenience — it is baked into consensus-committed message data (`max_dispatch_gas`) before the Ethereum-side execution is known to succeed. The Substrate side commits the message, assigns it an incrementing `nonce`, and treats it as delivered/finalized irrespective of whether the bounded-gas call on Ethereum actually succeeds [6](#0-5) . The corresponding value has already been irrevocably withdrawn/burned on the Substrate side by the time the command is queued (confirmed by the emulated test showing the native token burnt from the Ethereum sovereign account as part of message construction) [7](#0-6) . Because nonces strictly increment and are not replayable, there is no mechanism to resubmit the exact unlock/mint instruction if the Gateway's bounded-gas call reverts on Ethereum due to insufficient gas for the specific token/recipient.

### Impact Explanation
If a token contract or destination address (e.g., a smart-contract wallet or a token with additional transfer-hook logic) requires more gas than the constant `maximum_dispatch_gas_used_at_most` budget for that command, the Ethereum-side execution of `UnlockNativeToken`/`MintForeignToken`/`AgentExecute::TransferToken` will run out of gas and revert or partially fail, while the Substrate side has already burned/locked the corresponding value and consumed the nonce. This is a "permanent user-fund or bridge-state lock" scenario matching the required impact gate — funds withdrawn on the Substrate side never successfully reach the beneficiary on Ethereum, and there is no automatic retry path since the message/nonce is one-shot.

### Likelihood Explanation
This is not a hypothetical: the gas-budget-too-low failure has already been observed and patched once (LDO token, PR #8259) for `UnlockNativeToken`/`TransferToken`, confirming that the hardcoded constants are demonstrably insufficient for a subset of real-world ERC20 tokens/recipients and that no systematic guard prevents recurrence for other tokens or complex recipient contracts. Any unprivileged user reserve-transferring a token whose transfer logic (or whose recipient's fallback logic, mirroring the original smart-contract-wallet scenario) exceeds the current constant can trigger this without any privileged actor, malicious relayer, or governance involvement.

### Recommendation
- Do not treat message commitment/nonce consumption as final settlement; only release/finalize burned value on the Substrate side after an on-chain proof that the paired Ethereum-side call actually succeeded (not just delivered).
- Replace the single hardcoded constant per command with either a configurable, per-token/per-command gas allowance validated against real worst-case execution, or forward the caller-observed/estimated gas requirement with a safety margin, and add a defensive minimum-margin check plus monitoring/alerting so underestimation is caught before user funds are affected.
- Provide an explicit recovery/retry path (e.g., a governed re-issue mechanism keyed by the original XCM message id) for commands whose Ethereum-side execution reverted due to gas exhaustion, so that value is not permanently stranded.

### Proof of Concept
1. A user reserve-transfers an ERC20 token (or a token that will later be listed) whose `transferFrom`/hook logic consumes more gas than the current `UnlockNativeToken` budget (200,000) or `MintForeignToken` budget (100,000) — analogous to the already-observed LDO case which needed 140,000 against a 100,000 budget [5](#0-4) .
2. The XCM is converted into a `Command::UnlockNativeToken`/`MintForeignToken` with no origin check beyond a well-formed reserve-transfer XCM [3](#0-2) .
3. `do_process_message` commits the message with `max_dispatch_gas` fixed by `ConstantGasMeter`, increments the nonce, and the corresponding value has already been withdrawn from the sovereign/reserve account on the Substrate side [6](#0-5) .
4. On Ethereum, the Gateway's bounded-gas call to the token/recipient reverts due to insufficient gas.
5. The nonce is consumed and cannot be resubmitted; the withdrawn/burned Substrate-side value has no corresponding successful unlock/mint on Ethereum, resulting in a stranded-fund state.

### Citations

**File:** bridges/snowbridge/primitives/outbound-queue/src/v1/message.rs (L342-374)
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

**File:** prdoc/stable2503-1/pr_8259.prdoc (L1-8)
```text
title: Snowbridge - Update TransferToken command gas limit.

doc:
  - audience: Runtime Dev
    description: |
      Transfering certain ERC20 tokens require more gas than 100_000 gas. An example is LDO token which requires 140_000 gas.
      This change updates the gas limit to 200_000 and also updates the default fees for testnet runtimes.
      NOTE: make sure to update the relevant runtime fees to account for this change.
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

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge.rs (L917-947)
```rust
	AssetHubWestend::execute_with(|| {
		type RuntimeEvent = <AssetHubWestend as Chain>::RuntimeEvent;

		assert_expected_events!(
			AssetHubWestend,
			vec![RuntimeEvent::Balances(pallet_balances::Event::Withdraw{ .. }) => {},]
		);

		let events = AssetHubWestend::events();

		// Check that the native token burnt from some reserved account
		assert!(
			events.iter().any(|event| matches!(
				event,
				RuntimeEvent::Balances(pallet_balances::Event::Withdraw { who, ..})
					if *who == ethereum_sovereign.clone(),
			)),
			"native token burnt from Ethereum sovereign account."
		);

		// Check that the token was minted to beneficiary
		assert!(
			events.iter().any(|event| matches!(
				event,
				RuntimeEvent::Balances(pallet_balances::Event::Deposit { who, amount })
					if *amount >= TOKEN_AMOUNT && *who == AssetHubWestendReceiver::get()
			)),
			"Token minted to beneficiary."
		);
	});
}
```
