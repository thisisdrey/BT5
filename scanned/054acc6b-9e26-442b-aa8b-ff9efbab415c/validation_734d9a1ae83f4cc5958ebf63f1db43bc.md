### Title
Unvalidated, User-Controlled `gas` Field in Snowbridge V2 `CallContract` Command Enables Underpriced Ethereum Dispatch Work - (File: `bridges/snowbridge/primitives/outbound-queue/src/v2/message.rs`)

### Summary
The external report's core issue is that user-supplied numeric parameters are cast/used directly without validating bounds, letting an attacker submit extreme values that desynchronize cost from resource consumption. The Snowbridge V2 outbound pipeline has an analogous pattern: the `gas` field of a `ContractCall::V1` XCM `Transact` payload is decoded from arbitrary user input and used verbatim, with no upper bound, as the message's `maximum_dispatch_gas_used_at_most`, while the fee charged for the message is computed from a wholly separate, attacker-controlled `PayFees` amount.

### Finding Description
Any XCM origin permitted by `AllowedAliasOrigin` (which can include ordinary parachain-origin senders, not just governance) can build a `Transact` instruction whose payload decodes into a `ContractCall::V1 { gas: u64, .. }`. This is done entirely inside `XcmConverter::convert`, which decodes the transact payload and pushes it into the outbound `Command::CallContract` without any bound check on `gas`: [1](#0-0) 

The `gas` value is then used directly, unclamped, as the dispatch gas estimate for the message: [2](#0-1) 

This differs from every other `Command` variant, which use fixed, hard-coded constant gas figures (`40_000`, `200_000`, `1_200_000`, etc.) derived from actual gas-report measurements — only `CallContract` inherits its gas figure straight from user input (`ContractCall::V1.gas`, itself a raw `u64` decoded from the XCM `Transact` call bytes): [3](#0-2) 

Meanwhile, the fee attached to the message (used to reward the relayer/pay for Ethereum-side execution) is derived independently from the `PayFees`/remote-fee XCM assets in the same message, via `extract_remote_fee()`, and is not tied to or validated against the declared `gas` value: [4](#0-3) 

Because `gas` (up to `u64::MAX`) and `fee` are set from two unrelated user-controlled inputs with no cross-validation, an attacker can submit a message declaring an enormous `max_dispatch_gas` while paying a minimal `fee`. This value flows unchanged into the committed message that is delivered on-chain to relayers/Ethereum: [5](#0-4) 

### Impact Explanation
This is the "public underpriced work" class explicitly called out in the impact gate: the dispatch-gas budget promised to Ethereum-side execution is not backed by a commensurate fee, because the two values are independently attacker-chosen. This degrades the bridge's economic model — relayers are asked to guarantee/execute against arbitrarily large declared gas limits for messages that pay negligible fees, disincentivizing relaying (stalling bridge processing) or, if a relayer executes anyway, exposes them to unrecoverable cost since the reward is fixed at message-creation and cannot reflect true execution cost. Since `CallContract` is reachable from any AH/parachain XCM origin permitted to alias (not privileged governance), this is an unprivileged-attacker path with direct bridge-processing impact, matching the gate's underpriced-work / stalled-bridge-processing criteria.

### Likelihood Explanation
Medium. The `Transact`/`CallContract` path is a documented, intended feature of Snowbridge V2 (arbitrary contract calls via the AliasOrigin flow), reachable by any account whose origin passes `AllowedAliasOrigin`. No malicious relayer, validator, or governance actor is required — only crafting an XCM message with an inflated `gas` value and a low `PayFees` amount. The main uncertainty (not fully verified from available context) is the exact configuration of `AllowedAliasOrigin` in production runtimes (e.g. bridge-hub-westend) and whether `extract_remote_fee`'s minimum-fee enforcement indirectly bounds this — this needs runtime-level confirmation.

### Recommendation
- Enforce an explicit maximum bound on `ContractCall::V1.gas` (e.g., a `MaxCallContractGas` config constant) in `XcmConverter::convert` before constructing `Command::CallContract`.
- Tie the fee/reward computation to the declared `gas` (as is done via `fee_per_gas` for other commands in v1), so `fee < gas * fee_per_gas` is rejected, rather than treating `fee` and `gas` as independent user inputs.
- Add a regression test asserting that oversized `gas` values are rejected or fee-adjusted at conversion time.

### Proof of Concept
1. On a parachain permitted by `AllowedAliasOrigin`, build an XCM with `WithdrawAsset`, `PayFees` (minimal ETH amount), `AliasOrigin`, `DepositAsset`, and a `Transact` instruction whose call decodes to `ContractCall::V1 { target, calldata: vec![], value: 0, gas: u64::MAX }`, followed by `SetTopic`.
2. Submit via `PolkadotXcm::execute`/`send` as shown in the existing test harness pattern: [6](#0-5) 
3. `XcmConverter::convert` accepts the message and constructs `Command::CallContract { gas: u64::MAX, .. }` with no bound check.
4. `ConstantGasMeter::maximum_dispatch_gas_used_at_most` returns `u64::MAX` verbatim, and this value is committed into `CommittedMessage`/`OutboundMessage.max_dispatch_gas` while the attached `fee` remains whatever minimal `PayFees` amount the attacker chose — demonstrating the decoupling between promised gas work and paid fee.

### Citations

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs (L217-220)
```rust
	pub fn convert(&mut self) -> Result<Message, XcmConverterError> {
		// Get fee amount
		let fee_amount = self.extract_remote_fee()?;

```

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs (L294-305)
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

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/mod.rs (L20-31)
```rust
#[derive(Clone, Encode, Decode, PartialEq, Debug, TypeInfo)]
pub enum ContractCall {
	V1 {
		/// Target contract address
		target: [u8; 20],
		/// ABI-encoded calldata
		calldata: Vec<u8>,
		/// Include ether held by the agent contract
		value: u128,
		/// Maximum gas to forward to target contract
		gas: u64,
	},
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L330-343)
```rust

			let digest_item: DigestItem = SnowbridgeDigestItem::SnowbridgeV2(root).into();

			// Insert merkle root into the header digest
			<frame_system::Pallet<T>>::deposit_log(digest_item);

			T::OnNewCommitment::on_new_commitment(root);

			Self::deposit_event(Event::MessagesCommitted { root, count });
		}

		/// Process a message delivered by the MessageQueue pallet.
		/// IMPORTANT!! This method does not roll back storage changes on error.
		pub(crate) fn do_process_message(
```

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound.rs (L543-572)
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
```
