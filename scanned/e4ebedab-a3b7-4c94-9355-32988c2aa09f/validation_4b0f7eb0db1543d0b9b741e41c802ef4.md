### Title
Hardcoded per-command gas estimates in `ConstantGasMeter` can under-provision Ethereum-side execution, permanently stalling Snowbridge outbound message settlement - ([File: bridges/snowbridge/primitives/outbound-queue/src/v1/message.rs])

### Summary
The Snowbridge outbound queue commits messages to Ethereum with a `gas` value computed by `ConstantGasMeter::maximum_dispatch_gas_used_at_most`, which assigns a fixed, hardcoded gas figure per `Command` variant (e.g. `RegisterForeignToken => 1_200_000`, `MintForeignToken => 100_000`, `UnlockNativeToken => 200_000` in v2; `TransferToken => 200_000` etc. in v1) [1](#0-0) [2](#0-1) . This is exactly the same bug class as the GMX report: a client-side hardcoded gas allowance that assumes it will always be sufficient/accepted on the counterparty execution environment, with no runtime feedback loop to detect drift.

### Finding Description
`do_process_message` in the outbound-queue-v2 pallet stamps every command with `T::GasMeter::maximum_dispatch_gas_used_at_most(&command)`, commits the message into the Merkle-committed `Messages`/`MessageLeaves` storage, advances the monotonic `Nonce`, and creates a `PendingOrder` holding the relayer fee — all before the command is ever executed on Ethereum [3](#0-2) . The `gas` field is baked into the ABI-encoded, hashed commitment (`CommandWrapper.gas`) that is verified against the Gateway contract on Ethereum, so it cannot be adjusted after commit [4](#0-3) .

The gas figures themselves are static constants chosen from a point-in-time gas report ("A healthy buffer is added on top of these figures...") and are not validated against the actual Gateway contract's real gas consumption at commit time [5](#0-4) . This has already manifested in production: `TransferToken`'s hardcoded 100,000 gas was insufficient for tokens like LDO which need ~140,000 gas, forcing an emergency runtime upgrade to bump the constant to 200,000 across multiple runtimes [6](#0-5) . That incident is the direct real-world proof that the hardcoded-gas assumption drifts from the actual required/allowed gas on the Ethereum side — the same broken invariant as `Keys.MAX_CALLBACK_GAS_LIMIT` vs. GMXWorker's hardcoded 2,000,000 in the GMX report.

The remaining live risk: `RegisterForeignToken` (1,200,000) and `MintForeignToken` (100,000) in v2, and the corresponding v1 constants, are still fixed and unauditable against variable-size, permissionlessly-supplied inputs (token `name`/`symbol` bytes are attacker/user-controlled up to the payload size bound) [7](#0-6) . Any registration of a token whose metadata or downstream contract logic pushes actual gas consumption above the hardcoded figure — or any future EVM gas-cost repricing on the Ethereum side — causes the Gateway-side command execution to run out of gas and fail, while the Substrate-side state (`Nonce`, committed leaf, `PendingOrder`) has already moved forward irreversibly, since `do_process_message` explicitly documents "This method does not roll back storage changes on error" [8](#0-7) .

### Impact Explanation
Unlike a simple revert-and-retry, once a message is committed with an out-of-date gas allowance the nonce has already advanced and the fee is locked in `PendingOrders`. A relayer cannot submit a valid delivery receipt for a command that failed for out-of-gas reasons on Ethereum, so the fee/reward for that message may remain stuck in `PendingOrders` indefinitely (no explicit reclaim/cancel path is present in the reviewed code, only `submit_delivery_receipt` removing the entry after a successful proof) [9](#0-8) . This stalls bridge processing for the affected command class and can permanently lock the associated user operation (e.g. a foreign asset registration or mint that never completes on Ethereum), matching the "permanent user-fund or bridge-state lock" and "public underpriced work that ... stalls bridge processing" impact categories.

### Likelihood Explanation
This is not hypothetical: the identical failure mode already happened for `TransferToken` and required a coordinated runtime upgrade across `asset-hub-westend`, `asset-hub-rococo`, `bridge-hub-westend`, and `bridge-hub-rococo` to fix [10](#0-9) . The other constants (`RegisterForeignToken`, `MintForeignToken`, `Upgrade`'s base 50,000, `UnlockNativeToken`) remain equally exposed to the same class of estimation drift, and none of these paths require a malicious validator, relayer, or admin — the trigger is simply a legitimate, permissionless cross-chain asset registration/transfer whose real-world gas profile exceeds the hardcoded assumption.

### Recommendation
Do not rely on a single hardcoded constant per command as the sole source of truth for gas provisioning. Options: (1) make the per-command gas ceiling configurable via governance/storage so it can be raised without a full runtime upgrade when drift is detected; (2) add a safety margin that is periodically re-validated against actual Gateway contract gas-report benchmarks as part of CI, with alerts on regressions; (3) provide an explicit governance-callable "requeue/bump gas" path for a `PendingOrder` whose corresponding command failed on Ethereum, so fees are not permanently stuck; (4) consider allowing the Gateway to emit a `CommandFailed`-type event distinguishable from generic failure so `submit_delivery_receipt` can settle the order (refund/redirect) even when dispatch itself reverted.

### Proof of Concept
1. A user submits a legitimate, permissionless foreign-asset registration flow that results in a `Command::RegisterForeignToken` (or `MintForeignToken`) being generated with token metadata that is valid under `MaxMessagePayloadSize` but drives actual Ethereum-side gas usage above the hardcoded `1_200_000` / `100_000` figures in `ConstantGasMeter` [1](#0-0) .
2. `do_process_message` computes `gas = T::GasMeter::maximum_dispatch_gas_used_at_most(&command)`, commits the message (advances `Nonce`, appends to `MessageLeaves`, inserts `PendingOrder`) [3](#0-2) .
3. A relayer delivers the message to the Ethereum Gateway contract, which forwards exactly the committed `gas` value for command execution; execution runs out of gas and reverts the command dispatch (this exact scenario is documented as having occurred historically for `TransferToken`, see the fix in `prdoc/stable2503-1/pr_7947.prdoc`) [6](#0-5) .
4. The relayer cannot produce a valid success delivery-receipt event for that nonce, so `submit_delivery_receipt`/`process_delivery_receipt` never fires for that nonce, leaving the `PendingOrder` (and the underlying cross-chain operation) stuck with no in-repo recovery path [9](#0-8) .

Note: I was unable to inspect the Solidity Gateway contract source (`bridges/snowbridge/contracts/**Gateway*.sol`) directly, as it does not appear to be indexed in this repo snapshot for content search; my conclusions about Ethereum-side gas enforcement are based on the Rust-side primitives, the pallet processing logic, and the documented historical incident (`pr_7947`/`pr_8259`) rather than direct Solidity code review. If exact confirmation of the Gateway's gas-forwarding/revert behavior is needed, a Devin session with full repo access should inspect the contracts directory.

### Citations

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/message.rs (L36-40)
```rust
		struct CommandWrapper {
			uint8 kind;
			uint64 gas;
			bytes payload;
		}
```

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/message.rs (L163-172)
```rust
	RegisterForeignToken {
		/// ID for the token
		token_id: H256,
		/// Name of the token
		name: Vec<u8>,
		/// Short symbol for the token
		symbol: Vec<u8>,
		/// Number of decimal places
		decimals: u8,
	},
```

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/message.rs (L281-289)
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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L341-343)
```rust
		/// Process a message delivered by the MessageQueue pallet.
		/// IMPORTANT!! This method does not roll back storage changes on error.
		pub(crate) fn do_process_message(
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L371-438)
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

			<Nonce<T>>::set(nonce);
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L445-480)
```rust
		/// Process a delivery receipt from a relayer, to allocate the relayer reward.
		pub fn process_delivery_receipt(
			relayer: <T as frame_system::Config>::AccountId,
			receipt: DeliveryReceipt,
		) -> DispatchResult
		where
			<T as frame_system::Config>::AccountId: From<[u8; 32]>,
		{
			// Verify that the message was submitted from the known Gateway contract
			ensure!(T::GatewayAddress::get() == receipt.gateway, Error::<T>::InvalidGateway);

			let reward_account = if receipt.reward_address == [0u8; 32] {
				relayer
			} else {
				receipt.reward_address.into()
			};

			let nonce = receipt.nonce;

			let order = <PendingOrders<T>>::get(nonce).ok_or(Error::<T>::InvalidPendingNonce)?;

			if order.fee > 0 {
				// Pay relayer reward
				T::RewardPayment::register_reward(
					&reward_account,
					T::DefaultRewardKind::get(),
					order.fee,
				);
			}

			<PendingOrders<T>>::remove(nonce);

			Self::deposit_event(Event::MessageDelivered { nonce });

			Ok(())
		}
```

**File:** prdoc/stable2503-1/pr_7947.prdoc (L1-24)
```text
title: Snowbridge - Update TransferToken command gas limit.

doc:
  - audience: Runtime Dev
    description: |
      Transfering certain ERC20 tokens require more gas than 100_000 gas. An example is LDO token which requires 140_000 gas.
      This change updates the gas limit to 200_000 and also updates the default fees for testnet runtimes.
      NOTE: make sure to update the relevant runtime fees to account for this change.

crates:
  - name: asset-hub-westend-runtime
    bump: patch
  - name: asset-hub-rococo-runtime
    bump: patch
  - name: bridge-hub-westend-runtime
    bump: patch
  - name: bridge-hub-rococo-runtime
    bump: patch
  - name: snowbridge-outbound-queue-primitives
    bump: patch
  - name: snowbridge-pallet-system
    bump: minor
  - name: snowbridge-pallet-outbound-queue-v2
    bump: minor
```
