Audit Report

## Title
User-controlled `gas` field in `Command::CallContract` is trusted without a minimum bound, enabling gas-starvation of Snowbridge V2 outbound messages - (File: `bridges/snowbridge/primitives/outbound-queue/src/v2/message.rs`)

## Summary
`ConstantGasMeter::maximum_dispatch_gas_used_at_most` returns the caller-supplied `gas` field verbatim for `Command::CallContract`, unlike every other command variant which uses a protocol-chosen constant. This value is committed into the outbound message merkle root and consumed on Ethereum to bound gas forwarded to the target contract, with no minimum floor enforced anywhere in the send path.

## Finding Description
`ConstantGasMeter::maximum_dispatch_gas_used_at_most` computes the gas for each `Command`, using a hardcoded constant for `SetOperatingMode`, `Upgrade`, `UnlockNativeToken`, `RegisterForeignToken`, and `MintForeignToken`, but for `CallContract` it returns `*gas_limit` unmodified: [1](#0-0) 

This `gas` field originates from `ContractCall::V1 { gas, .. }`, decoded from an XCM `Transact` instruction's call bytes in `XcmConverter::convert`, and mapped directly into `Command::CallContract { target, calldata, gas, value }` with no bounds check: [2](#0-1) 

This XCM path is reachable by an ordinary signed account: the integration test `transact_with_agent_from_asset_hub` constructs exactly this flow via `RuntimeOrigin::signed(AssetHubWestendSender::get())` calling `PolkadotXcm::execute` with a `Transact` carrying `ContractCall::V1 { gas: 40000, .. }`, which is exported to BridgeHub's V2 outbound queue: [3](#0-2) 

`Pallet::do_process_message` in the outbound-queue-v2 pallet computes `gas: T::GasMeter::maximum_dispatch_gas_used_at_most(&command)` and commits it into `OutboundCommandWrapper`/`CommandWrapper` without any floor check: [4](#0-3)  No code in `XcmConverter::convert`, `EthereumBlobExporter::validate`, or `do_process_message` rejects or clamps a `CallContract` command whose `gas` is below any safe minimum.

## Impact Explanation
Because gas is fully unprivileged-user-controlled with no minimum, a signed AssetHub account can submit a `CallContract` command with an arbitrarily low `gas` (e.g. `gas: 1`), guaranteeing the forwarded call on Ethereum reverts due to insufficient gas, while the fee for the message has already been withdrawn/paid and the nonce is processed as delivered on the BridgeHub side, matching the "public underpriced work that degrades... stalls bridge processing" pattern named in the impact gate — the destination-side call is guaranteed to fail for a cost the user or protocol already paid, with no local enforcement to prevent it.

## Likelihood Explanation
Likelihood is high: any signed account on AssetHub can trigger this via `PolkadotXcm::execute` with a `Transact` instruction encoding `ContractCall::V1 { gas: <low value>, .. }`, as demonstrated by the existing integration test scaffolding for `transact_with_agent_from_asset_hub`, requiring no privileged origin, governance, or `AliasOrigin` bypass — only the `AllowedAliasOrigin` filter check on the alias, which does not constrain the `gas` field at all.

## Recommendation
Enforce a protocol-defined minimum gas floor for `Command::CallContract` in `ConstantGasMeter::maximum_dispatch_gas_used_at_most` (e.g., `max(*gas_limit, MIN_CALL_CONTRACT_GAS)`) or reject the message in `XcmConverter::convert`/`do_process_message` when `gas` is below a safe minimum before it is queued and committed.

## Proof of Concept
1. On AssetHub, as any signed account with sufficient balance, call `PolkadotXcm::execute` with an XCM containing `InitiateTransfer` to Ethereum whose `remote_xcm` includes `Transact { call: ContractCall::V1 { target, calldata, gas: 1, value }.encode() }`, mirroring `transact_with_agent_from_asset_hub` in `cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound.rs`.
2. On BridgeHub, `EthereumBlobExporter::validate` → `XcmConverter::convert` decodes this into `Command::CallContract { gas: 1, .. }` with no validation.
3. `do_process_message` computes `gas: T::GasMeter::maximum_dispatch_gas_used_at_most(&command)` = `1` and commits the message into `MessageLeaves`, charging/consuming the associated fee.
4. Once relayed to Ethereum, the forwarded call to `target` reverts for out-of-gas, while the nonce/message is treated as processed on the BridgeHub side.

### Citations

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

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs (L294-304)
```rust
		// Transact commands
		let transact_call = match_expression!(self.peek(), Ok(Transact { call, .. }), call);
		if let Some(transact_call) = transact_call {
			let _ = self.next();
			let transact =
				ContractCall::decode_all(&mut transact_call.clone().into_encoded().as_slice())
					.map_err(|_| TransactDecodeFailed)?;
			match transact {
				ContractCall::V1 { target, calldata, gas, value } => commands
					.push(Command::CallContract { target: target.into(), calldata, gas, value }),
			}
```

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound.rs (L543-580)
```rust
		let transact_info = ContractCall::V1 {
			target: Default::default(),
			calldata: vec![],
			gas: 40000,
			// value should be less than the transfer amount, require validation on BH Exporter
			value: 4 * (TOKEN_AMOUNT - REMOTE_FEE_AMOUNT_IN_ETHER) / 5,
		};

		let xcms = VersionedXcm::from(Xcm(vec![
			WithdrawAsset(assets.clone().into()),
			PayFees { asset: local_fee_asset.clone() },
			InitiateTransfer {
				destination: ethereum(),
				remote_fees: Some(AssetTransferFilter::ReserveWithdraw(Definite(
					remote_fee_asset.clone().into(),
				))),
				preserve_origin: true,
				assets: BoundedVec::truncate_from(vec![AssetTransferFilter::ReserveWithdraw(
					Definite(reserve_asset.clone().into()),
				)]),
				remote_xcm: Xcm(vec![
					DepositAsset { assets: Wild(AllCounted(2)), beneficiary },
					Transact {
						origin_kind: OriginKind::SovereignAccount,
						fallback_max_weight: None,
						call: transact_info.encode().into(),
					},
				]),
			},
		]));

		<AssetHubWestend as AssetHubWestendPallet>::PolkadotXcm::execute(
			RuntimeOrigin::signed(AssetHubWestendSender::get()),
			bx!(xcms),
			Weight::from(EXECUTION_WEIGHT),
		)
		.unwrap();
	});
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L371-415)
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
```
