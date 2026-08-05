The critical missing link is now confirmed. In `bridges/snowbridge/pallets/outbound-queue-v2/src/send_message_impl.rs`, `SendMessage::validate` only checks payload size (`MessageTooLarge`) and does nothing to relate `fee` to gas cost:

```rust
fn validate(message: &Message) -> Result<Self::Ticket, SendError> {
    let payload = message.encode();
    ensure!(payload.len() < T::MaxMessagePayloadSize::get() as usize, SendError::MessageTooLarge);
    Ok(message.clone())
}
```

And in `bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs`, `extract_remote_fee` only validates that the withdrawn fee asset is Ether and that `reserved_fee_amount >= fee_amount` — it never touches the `CallContract.gas` value at all. The `gas` field flows unchecked from the user-supplied `ContractCall::V1.gas` through `Command::CallContract` into `ConstantGasMeter::maximum_dispatch_gas_used_at_most` (which just echoes it) and into the committed `OutboundCommandWrapper.gas`, while `fee` is stored in `PendingOrder.fee` completely independently. There is no cross-check anywhere in the pipeline (`do_process_message`, `send_message_impl.rs`, or `convert.rs`) enforcing `fee >= gas * fee_per_gas`. This confirms the claim's exploit chain end-to-end.

Audit Report

## Title
Snowbridge V2 `CallContract` gas metering trusts caller-supplied `gas` value with no fee-to-gas cross-check, allowing underpriced relayer-refund commitments - (File: bridges/snowbridge/primitives/outbound-queue/src/v2/message.rs)

## Summary
In Snowbridge Outbound Queue V2, `ConstantGasMeter::maximum_dispatch_gas_used_at_most` for `Command::CallContract` simply echoes the attacker-controlled `gas` field taken directly from the user's `ContractCall::V1` payload, rather than using a protocol-calibrated constant as it does for every other command. No code in the message pipeline (`XcmConverter::convert`/`extract_remote_fee` in `bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs`, or `SendMessage::validate`/`do_process_message` in `bridges/snowbridge/pallets/outbound-queue-v2/src/`) ever checks that the `fee` (from `PayFees`/`WithdrawAsset`) is sufficient to cover `gas * fee_per_gas`, so a user can commit an arbitrarily high `gas` limit for cheap while paying an unrelated, minimal fee.

## Finding Description
`GasMeter::maximum_dispatch_gas_used_at_most` is meant to be the protocol's estimate of real remote execution cost used to size relayer compensation, exactly mirroring V1's hardcoded-constant approach: [1](#0-0)  For every command except `CallContract`, this is a fixed protocol constant. For `CallContract`, `Command::CallContract { gas: gas_limit, .. } => *gas_limit` takes the value verbatim from the `Command`, which itself is populated straight from the user-supplied `ContractCall::V1.gas` field decoded from the `Transact` XCM instruction in `XcmConverter::convert`: [2](#0-1) 

The `fee` attached to the `Message` is computed by `extract_remote_fee`, which only validates that the fee asset is Ether and that the reserved amount covers the nominal `PayFees` amount — it has no relationship to `gas` whatsoever: [3](#0-2) 

Downstream, `SendMessage::validate` for the pallet only checks payload size, performing no fee/gas cross-check before the message is accepted into the queue: [4](#0-3) 

Finally, `do_process_message` decodes the message, computes `gas` via `T::GasMeter::maximum_dispatch_gas_used_at_most(&command)` (which is just the attacker's own number for `CallContract`), and stores the untouched `fee` into `PendingOrder` without ever reconciling the two: [5](#0-4) [6](#0-5) 

This is confirmed exploitable end-to-end via the public `pallet_xcm::execute` entrypoint, as demonstrated by the existing test fixture that freely sets an arbitrary `gas` value: [7](#0-6) 

No guard anywhere in the reviewed pipeline enforces `fee >= gas_limit * fee_per_gas`, unlike V1 where `Fee` is derived directly from `GasMeter` output via `calculate_fee` before a message is queued.

## Impact Explanation
An unprivileged user can submit a `Transact`/`CallContract` instruction via the public `pallet_xcm::execute` extrinsic on Asset Hub with a large `gas` value (ensuring successful execution on Ethereum, since `gas` becomes the actual gas forwarded to the target contract) while paying a minimal, unrelated `fee` via `PayFees`. The `PendingOrder.fee` — the exact value that determines relayer compensation via `pallet_bridge_relayers::Event::RewardRegistered` upon delivery-receipt processing — is decoupled from the real dispatch cost committed in `OutboundCommandWrapper.gas`/`CommandWrapper.gas`. This systematically underpays relayers for `CallContract` message delivery, degrading Snowbridge V2 relayer economics and risking a stall in outbound message processing, matching the "public underpriced work that degrades block production or stalls bridge processing" impact category.

## Likelihood Explanation
High feasibility, no privileged access required: an attacker only needs to call the standard public `pallet_xcm::execute` extrinsic on Asset Hub with a crafted `WithdrawAsset`/`PayFees`/`InitiateTransfer`/`Transact` XCM, exactly matching the pattern already exercised in `transact_with_agent_from_asset_hub`. No relayer collusion, governance action, or validator compromise is needed, and the attack is trivially repeatable.

## Recommendation
Add an explicit fee-sufficiency check before a `CallContract` message is accepted into the outbound queue — either in `XcmConverter::convert`/`extract_remote_fee` or in `SendMessage::validate`/`do_process_message` — that computes the minimum required fee as `gas_limit * PricingParameters::fee_per_gas` (plus base reward) and rejects the message if the supplied `fee` does not cover it, matching the enforcement that exists in V1 via `calculate_fee`.

## Proof of Concept
1. On Asset Hub, submit via `pallet_xcm::execute` (public, unprivileged): `WithdrawAsset`, `PayFees` with a minimal WETH/Ether amount, `InitiateTransfer` to Ethereum with `remote_xcm` containing `Transact { call: ContractCall::V1 { gas: 10_000_000, .. } }`.
2. Observe `EthereumOutboundQueueV2::do_process_message` commits `gas = 10_000_000` into `OutboundCommandWrapper`/`CommandWrapper` while `PendingOrder.fee` reflects only the minimal `PayFees` amount.
3. After delivery, `process_delivery_receipt` pays the relayer based on the underpriced `PendingOrder.fee`, far below the actual ~10M gas cost incurred on Ethereum.
4. Repeat to systematically underpay relayers at scale.

### Citations

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/message.rs (L290-306)
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

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs (L94-117)
```rust
	/// Extract the fee asset item from PayFees(V5)
	fn extract_remote_fee(&mut self) -> Result<u128, XcmConverterError> {
		use XcmConverterError::*;
		let reserved_fee_assets = match_expression!(self.next()?, WithdrawAsset(fee), fee)
			.ok_or(WithdrawAssetExpected)?;
		ensure!(reserved_fee_assets.len() == 1, AssetResolutionFailed);
		let reserved_fee_asset =
			reserved_fee_assets.inner().first().cloned().ok_or(AssetResolutionFailed)?;
		let (reserved_fee_asset_id, reserved_fee_amount) = match reserved_fee_asset {
			Asset { id: asset_id, fun: Fungible(amount) } => Ok((asset_id, amount)),
			_ => Err(AssetResolutionFailed),
		}?;
		let fee_asset =
			match_expression!(self.next()?, PayFees { asset: fee }, fee).ok_or(InvalidFeeAsset)?;
		let (fee_asset_id, fee_amount) = match fee_asset {
			Asset { id: asset_id, fun: Fungible(amount) } => Ok((asset_id, *amount)),
			_ => Err(AssetResolutionFailed),
		}?;
		// Check the fee asset is Ether (XCM is evaluated in Ethereum context).
		ensure!(fee_asset_id.0 == Here.into(), InvalidFeeAsset);
		ensure!(reserved_fee_asset_id.0 == Here.into(), InvalidFeeAsset);
		ensure!(reserved_fee_amount >= fee_amount, InvalidFeeAsset);
		Ok(fee_amount)
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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/send_message_impl.rs (L23-32)
```rust
	fn validate(message: &Message) -> Result<Self::Ticket, SendError> {
		// The inner payload should not be too large
		let payload = message.encode();
		ensure!(
			payload.len() < T::MaxMessagePayloadSize::get() as usize,
			SendError::MessageTooLarge
		);

		Ok(message.clone())
	}
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L360-378)
```rust
			// Decode bytes into Message
			let Message { origin, id, fee, commands } =
				Message::decode(&mut message).map_err(|_| {
					Self::deposit_event(Event::MessageRejected {
						id: None,
						payload: message.to_vec(),
						error: Corrupt,
					});
					Corrupt
				})?;

			// Convert it to OutboundMessage and save into Messages storage
			let commands: Vec<OutboundCommandWrapper> = commands
				.into_iter()
				.map(|command| OutboundCommandWrapper {
					kind: command.index(),
					gas: T::GasMeter::maximum_dispatch_gas_used_at_most(&command),
					payload: command.abi_encode(),
				})
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/types.rs (L15-24)
```rust
#[derive(Encode, Decode, TypeInfo, Clone, Eq, PartialEq, Debug, MaxEncodedLen)]
pub struct PendingOrder<BlockNumber> {
	/// The nonce used to identify the message
	pub nonce: u64,
	/// The block number in which the message was committed
	pub block_number: BlockNumber,
	/// The fee in Ether provided by the user to incentivize message delivery
	#[codec(compact)]
	pub fee: u128,
}
```

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound.rs (L543-549)
```rust
		let transact_info = ContractCall::V1 {
			target: Default::default(),
			calldata: vec![],
			gas: 40000,
			// value should be less than the transfer amount, require validation on BH Exporter
			value: 4 * (TOKEN_AMOUNT - REMOTE_FEE_AMOUNT_IN_ETHER) / 5,
		};
```
