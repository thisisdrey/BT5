This is confirmed as an accurate, verifiable finding. Key evidence:

1. `ConstantGasMeter::maximum_dispatch_gas_used_at_most` hardcodes gas per command type (`200_000` for `UnlockNativeToken`, `100_000` for `MintForeignToken`), with no per-token override [1](#0-0) .
2. This value is committed irrevocably into the Merkle leaf/`OutboundMessage` at `do_process_message`, and the nonce/order is finalized regardless of the actual token's execution cost [2](#0-1) .
3. Critically, the `InboundMessageDispatched` event carries a `success: bool` field, confirming the Gateway contract design catches per-command execution failure (e.g., OOG) rather than reverting the whole relayer transaction [3](#0-2) .
4. `process_delivery_receipt` never inspects `receipt.success` — it unconditionally pays the relayer reward and removes the `PendingOrder` for the nonce regardless of whether the command actually executed successfully on Ethereum [4](#0-3) . This means there is no BridgeHub-side state that stays "pending" for a failed dispatch to allow a retry — the order is finalized either way, matching the claim's assertion that there is no in-repo path to resubmit a failed nonce with higher gas.
5. The v1 gas figure for the equivalent `TransferToken` operation was already bumped from `100_000` to `200_000` in production after the LDO token required `140_000` gas, corroborating that the constant-gas estimation approach has already caused underestimation once [5](#0-4) .

The burn/withdraw on the source chain (via `WithdrawAsset` in the XCM, processed by the `EthereumBlobExporter`/`XcmConverter`) happens as part of validating and committing the message, independent of and prior to any confirmation of Ethereum-side execution success [6](#0-5) .

Audit Report

## Title
Hardcoded per-command gas estimates in Snowbridge `ConstantGasMeter` can permanently under-price `UnlockNativeToken`/`MintForeignToken` execution on Ethereum, locking bridged funds with no retry path - (File: `bridges/snowbridge/primitives/outbound-queue/src/v2/message.rs`)

## Summary
The outbound-queue-v2 pallet assigns a fixed, hardcoded gas limit to `UnlockNativeToken` (`200_000`) and `MintForeignToken` (`100_000`) commands via `ConstantGasMeter::maximum_dispatch_gas_used_at_most`, independent of the actual ERC20 token's transfer/mint logic, and commits this gas figure irrevocably into the Merkle-committed `OutboundMessage` for a given nonce. Because delivery-receipt processing pays the relayer and finalizes the order regardless of the reported execution `success`, a token whose real gas cost exceeds the hardcoded constant will fail on Ethereum with the user's asset already withdrawn/burned on the source chain, with no mechanism to retry that nonce with a higher gas limit.

## Finding Description
When a user submits an XCM burning/withdrawing an asset on AssetHub destined for Ethereum, `XcmConverter`/`EthereumBlobExporter` builds `Command::UnlockNativeToken` or `Command::MintForeignToken` from the withdrawn assets. `OutboundQueue::do_process_message` then wraps the command with `gas: T::GasMeter::maximum_dispatch_gas_used_at_most(&command)`, which for `ConstantGasMeter` returns a fixed `200_000`/`100_000` regardless of the destination token's actual transfer/mint gas cost. This gas figure and the command are committed into `MessageLeaves` (via Keccak256 hash) and become part of the header digest merkle root that Ethereum's Gateway contract accepts as authoritative. Once committed, the nonce is advanced and a `PendingOrder` is recorded — this state is final for that nonce.

On the Ethereum side, `InboundMessageDispatched` carries a `success: bool` field, indicating the Gateway contract catches per-command dispatch failure (such as out-of-gas) without reverting the whole relayer transaction. When the relayer submits the corresponding delivery receipt, `process_delivery_receipt` does not inspect `receipt.success` at all: it unconditionally pays the relayer's fee (if `order.fee > 0`) and removes the `PendingOrder`, finalizing the message regardless of whether the underlying token operation actually succeeded on Ethereum. There is no code path in this pallet to reprocess a failed nonce with a different gas limit — the committed message and its gas figure are immutable once part of the Merkle root, and BridgeHub state does not distinguish success from failure once the receipt is processed.

This is the same underestimation bug class documented in `prdoc/stable2503-1/pr_7947.prdoc`, where the v1 equivalent (`AgentExecuteCommand::TransferToken`) gas figure of `100_000` was found insufficient for the LDO token (needed `140_000`) and had to be raised to `200_000` in production, confirming that a single constant does not safely bound the cost of arbitrary ERC20 transfer/mint logic (fee-on-transfer, hooks, blacklists, proxy patterns, etc.).

## Impact Explanation
This matches "permanent user-fund or bridge-state lock" from the Polkadot SDK impact gate. Any registered ERC20/foreign token whose real `transfer`/mint execution cost exceeds the hardcoded `200_000`/`100_000` gas causes on-Ethereum execution failure for the affected command, while the corresponding asset has already been withdrawn from the user's balance on the source chain as part of the same XCM that produced the command. Because `process_delivery_receipt` finalizes the nonce and pays the relayer regardless of `success`, there is no BridgeHub-side retry mechanism for that specific nonce — the user's withdrawn funds are not reflected on Ethereum and cannot be recovered through any code path in this pallet.

## Likelihood Explanation
Likelihood is high and requires no privileged actor: any unprivileged user bridging a foreign/native token whose transfer or mint logic is more gas-expensive than the hardcoded constant (a common characteristic among fee-on-transfer, rebasing, blacklist-checking, or proxy-based ERC20s) triggers this condition through the standard, permissionless AssetHub bridge-out flow. The project has already encountered this exact underestimation failure mode once in production (LDO token, PR #7947), demonstrating the constant is not a safe universal bound.

## Recommendation
- Replace the single constant per-command-type gas figure with a per-token gas allowance, similar to how `Command::CallContract` already carries a caller-supplied `gas` field, validated against the specific token registered via `RegisterForeignToken`.
- Track and periodically recalibrate gas requirements for tokens with non-standard transfer/mint logic.
- Add a mechanism (permissionless or governance-gated) to resubmit/bump the gas limit for an already-committed but undelivered/failed nonce, so that funds withdrawn on the source side are not permanently stranded when the estimate proves insufficient, and ensure `process_delivery_receipt` distinguishes `success == false` from `success == true` outcomes rather than unconditionally finalizing and rewarding both.

## Proof of Concept
1. Register a foreign/native ERC20 token whose `transfer`/mint logic (e.g., due to a transfer hook, blacklist check, or fee-on-transfer logic) costs more than `200_000` gas for `UnlockNativeToken` (or `100_000` for `MintForeignToken`).
2. A user performs a normal AssetHub bridge-out flow: XCM `WithdrawAsset` + `DepositAsset`, converted by `XcmConverter`/`extract_ethereum_native_assets` into `Command::UnlockNativeToken`.
3. `OutboundQueue::do_process_message` commits the message with `gas = 200_000` from `ConstantGasMeter::maximum_dispatch_gas_used_at_most`, and the user's asset has already been withdrawn on AssetHub as part of the XCM.
4. The relayer submits the committed message to the Gateway contract on Ethereum; the `UnlockNativeToken` dispatch reverts with out-of-gas, and the Gateway emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
5. The relayer submits the delivery receipt on BridgeHub; `process_delivery_receipt` does not check `success`, pays the relayer's fee, and removes the `PendingOrder`, permanently finalizing the nonce — with no mechanism in the pallet to resubmit the same nonce with a higher gas limit, permanently stranding the user's already-withdrawn funds.

### Citations

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

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/delivery_receipt.rs (L10-27)
```rust
sol! {
	event InboundMessageDispatched(uint64 indexed nonce, bytes32 topic, bool success, bytes32 reward_address);
}

/// Delivery receipt
#[derive(Clone, Debug)]
pub struct DeliveryReceipt {
	/// The address of the outbound queue on Ethereum that emitted this message as an event log
	pub gateway: H160,
	/// The nonce of the dispatched message
	pub nonce: u64,
	/// Message topic
	pub topic: H256,
	/// Delivery status
	pub success: bool,
	/// The reward address
	pub reward_address: [u8; 32],
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
