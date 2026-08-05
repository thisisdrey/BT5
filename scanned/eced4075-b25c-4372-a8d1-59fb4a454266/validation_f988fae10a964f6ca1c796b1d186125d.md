## Analysis

The external report's core broken invariant: **a fixed, hardcoded gas-limit estimate for cross-chain message execution can be lower than the real execution cost of the destination-side operation, causing execution failure (OOG) after the source-side state change (burn/lock) has already been committed.**

The direct analog in this repository is Snowbridge's outbound message gas-metering scheme.

### Title
Hardcoded per-command gas estimates in Snowbridge `ConstantGasMeter` can permanently under-price `UnlockNativeToken`/`MintForeignToken` execution on Ethereum, locking bridged funds with no retry path - (File: `bridges/snowbridge/primitives/outbound-queue/src/v2/message.rs`)

### Summary
When a user burns/reserves an asset on AssetHub for delivery to Ethereum, the XCM converter builds a `Command::UnlockNativeToken` or `Command::MintForeignToken` [1](#0-0) . The outbound-queue-v2 pallet then assigns that command a **constant, hardcoded gas figure** via `ConstantGasMeter::maximum_dispatch_gas_used_at_most` — `200_000` for `UnlockNativeToken`, `100_000` for `MintForeignToken` [2](#0-1)  — and commits it irreversibly into the Merkle leaf/`OutboundMessage` that becomes the Ethereum-side execution instruction [3](#0-2) . This is exactly the same bug class as the report: a fixed cost estimate that does not account for token-specific execution cost (fee-on-transfer tokens, transfer hooks, blacklist checks, non-standard ERC20 logic, proxy/registry lookups), which was already proven insufficient once in production — the project had to bump the equivalent v1 gas figure from `100_000` to `200_000` after LDO required `140_000` gas [4](#0-3) .

### Finding Description
The flow is:
1. A user submits an XCM burning/reserving an asset on AssetHub destined for Ethereum.
2. `extract_ethereum_native_assets` / `extract_polkadot_native_assets` builds the corresponding `Command::UnlockNativeToken` / `Command::MintForeignToken` [5](#0-4) .
3. `OutboundQueue::do_process_message` wraps the command with `gas: T::GasMeter::maximum_dispatch_gas_used_at_most(&command)` and commits it as a Merkle leaf, advancing the pallet nonce and recording a `PendingOrder` [6](#0-5) .
4. `ConstantGasMeter` returns a fixed value regardless of the target ERC20's actual transfer/mint logic [7](#0-6) .

Because the message and its gas figure are committed into the Merkle root at step 3 (immutable, part of consensus state), there is no mechanism in this pallet to increase the gas limit for an already-committed nonce if it proves insufficient on Ethereum. Unlike the external LayerZero report — where an under-gassed `lzReceive()` at least stores the payload hash for retry with a higher gas limit — Snowbridge's committed `OutboundMessage.gas` for a given nonce is fixed forever once accepted into `MessageLeaves`. If the real gas cost of `UnlockNativeToken`/`MintForeignToken` on a particular ERC20 (e.g., a token with a transfer hook, blacklist check, or rebasing logic) exceeds the hardcoded `200_000`/`100_000` gas, the relayed execution reverts with out-of-gas on Ethereum, while the corresponding asset has already been burned/withdrawn on the Polkadot side (the XCM `WithdrawAsset`/burn precedes the outbound message and is not conditioned on Ethereum-side success).

### Impact Explanation
This directly matches "public underpriced work that degrades ... bridge processing" and "permanent user-fund or bridge-state lock" from the impact gate: any unprivileged user who bridges an ERC20 token whose real transfer/mint gas cost exceeds the hardcoded constant can trigger execution failure on the Ethereum side for their own message, with no in-repo path to retry that specific nonce with a higher gas limit — the burn/withdrawal on the Polkadot side already happened. This is worse than the reference bug (which at least supports retry), and the exact same underestimation vector (`100_000` for a token operation) was already proven exploitable once in production for the v1 `TransferToken`/`AgentExecute` path.

### Likelihood Explanation
Likelihood is high: the attacker primitive is simply registering/bridging an ERC20 that has a heavier-than-average `transfer`/`mint` implementation (which is common — fee-on-transfer tokens, rebasing tokens, tokens with allow/blacklists, proxy-based tokens) and initiating a normal, permissionless bridge transfer. No privileged actor, relayer collusion, or governance action is required — this is a standard user-triggered public entrypoint (AssetHub XCM burn → outbound queue).

### Recommendation
- Do not use a single constant gas figure for `UnlockNativeToken`/`MintForeignToken`; measure/allow per-token gas overrides (similar to how `CallContract` already carries a caller-supplied `gas` field) and validate against the specific token/asset registered via `RegisterForeignToken`.
- Add a governance or permissionless mechanism to bump the gas limit of an already-committed, undelivered nonce (analogous to LayerZero's retry-with-higher-gas), so that funds already withdrawn on the source side are not permanently stranded if the original estimate proves insufficient.
- Track real-world Ethereum gas usage per registered token and periodically recalibrate `ConstantGasMeter`, as was already found necessary once (pr_7947).

### Proof of Concept
1. Register a foreign ERC20 token whose `transfer`/hook logic costs > `200_000` gas for `UnlockNativeToken` (e.g., a token with `beforeTransfer` callback, blacklist, or fee logic) via `RegisterForeignToken`.
2. A user calls the normal AssetHub bridge-out flow (XCM `WithdrawAsset`+`DepositAsset` converted through `make_unlock_native_token_command`/`extract_ethereum_native_assets`) [8](#0-7) .
3. `OutboundQueue::do_process_message` commits the message with `gas = 200_000` from `ConstantGasMeter` [9](#0-8) , and the user's asset is already burned/reserved on AssetHub as part of the XCM.
4. When the relayer submits this committed message to the Gateway contract on Ethereum, execution reverts with out-of-gas because actual token transfer cost exceeds `200_000`.
5. The nonce is consumed and the message is final; there is no code path in this pallet to resubmit the same nonce with a higher gas limit, permanently stranding the user's already-withdrawn funds.

### Citations

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs (L119-186)
```rust
	/// Extract ethereum native assets
	fn extract_ethereum_native_assets(
		&mut self,
		enas: &Assets,
		deposit_assets: &AssetFilter,
		recipient: H160,
	) -> Result<Vec<Command>, XcmConverterError> {
		let mut commands: Vec<Command> = Vec::new();
		for ena in enas.clone().into_inner().into_iter() {
			// Check the the deposit asset filter matches what was reserved.
			if !deposit_assets.matches(&ena) {
				return Err(FilterDoesNotConsumeAllAssets);
			}

			// only fungible asset is allowed
			let (token, amount) = match ena {
				Asset { id: AssetId(inner_location), fun: Fungible(amount) } => {
					match inner_location.unpack() {
						(0, [AccountKey20 { network, key }]) if self.network_matches(network) => {
							Ok((H160(*key), amount))
						},
						// To allow ether
						(0, []) => Ok((H160([0; 20]), amount)),
						_ => Err(AssetResolutionFailed),
					}
				},
				_ => Err(AssetResolutionFailed),
			}?;

			// transfer amount must be greater than 0.
			ensure!(amount > 0, ZeroAssetTransfer);

			commands.push(Command::UnlockNativeToken { token, recipient, amount });
		}
		Ok(commands)
	}

	/// Extract polkadot native assets
	fn extract_polkadot_native_assets(
		&mut self,
		pnas: &Assets,
		deposit_assets: &AssetFilter,
		recipient: H160,
	) -> Result<Vec<Command>, XcmConverterError> {
		let mut commands: Vec<Command> = Vec::new();
		ensure!(pnas.len() > 0, NoReserveAssets);
		for pna in pnas.clone().into_inner().into_iter() {
			if !deposit_assets.matches(&pna) {
				return Err(FilterDoesNotConsumeAllAssets);
			}

			// Only fungible is allowed
			let Asset { id: AssetId(asset_id), fun: Fungible(amount) } = pna else {
				return Err(AssetResolutionFailed);
			};

			// transfer amount must be greater than 0.
			ensure!(amount > 0, ZeroAssetTransfer);

			// Ensure PNA already registered
			let token_id = TokenIdOf::convert_location(&asset_id).ok_or(InvalidAsset)?;
			let expected_asset_id = ConvertAssetId::maybe_convert(token_id).ok_or(InvalidAsset)?;
			ensure!(asset_id == expected_asset_id, InvalidAsset);

			commands.push(Command::MintForeignToken { token_id, recipient, amount });
		}
		Ok(commands)
	}
```

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/message.rs (L289-306)
```rust
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
