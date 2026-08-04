## Title
Unrestricted arbitrary contract call and ether draw via `Command::CallContract` in Snowbridge V2 outbound `Transact` — (File: `bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs`)

### Summary
The Pickle Finance report describes a proxy/voter contract (`crv-locker.sol` / `strategy-base.sol`) that lets a semi-trusted actor supply an arbitrary `_target` and arbitrary call `_data`, letting it call any function of any contract "on behalf of" the protocol contract, including transferring ETH held by that contract. Snowbridge's V2 outbound pipeline has the same broken invariant: any XCM sender whose origin passes the `AliasOrigin` filter can embed a `Transact` instruction carrying a `ContractCall::V1{ target, calldata, value, gas }` payload. This is converted, with no restriction on `target`, `calldata`, or `value`, into `Command::CallContract`, which the Ethereum Gateway will execute using the agent contract's identity and — per the `value` field's own doc comment — "ether held by the agent contract."

### Finding Description
`ContractCall::V1` is explicitly documented as an arbitrary call primitive: [1](#0-0) 

The XCM→command converter accepts this payload from any XCM message that satisfies only the `AllowedAliasOrigin` gate (an origin-shape check, not a target/value check) and forwards `target`, `calldata`, `gas`, and `value` verbatim into `Command::CallContract` with **no validation** of the target address, no allow-list of callable contracts, and no bound relating `value` to funds actually deposited/reserved in this message: [2](#0-1) [3](#0-2) 

Notably, the converter declares an error variant `CallContractValueInsufficient` in its error enum, implying an intended check that `value` must be backed by amounts actually transferred in the message — but this variant is never referenced/used inside `convert()`: [4](#0-3) 

The resulting `Command::CallContract` is ABI-encoded and dispatched to Ethereum as-is; its documentation confirms `value` draws on ether already "held by agent contract" rather than being limited to newly bridged funds: [5](#0-4) [6](#0-5) 

An integration test even encodes this same intent in a comment ("value should be less than the transfer amount, require validation on BH Exporter") while exercising the path, but no such enforcement is present in the converter code reviewed: [7](#0-6) 

### Impact Explanation
Any unprivileged account whose location can legitimately alias (e.g., a normal signed account on AssetHub aliasing into its own agent, which any account can trigger via `pallet_xcm::execute`) can construct a `ContractCall::V1` with:
- `target` = any Ethereum address (not restricted to the sender's own contracts),
- `calldata` = arbitrary ABI-encoded call,
- `value` = arbitrary amount, up to whatever ether balance the agent contract currently holds (accumulated from unclaimed fees, dust, or unrelated prior operations),

and have the Gateway execute that call on Ethereum under the agent's identity, moving/spending the agent's held ether or invoking arbitrary state-changing functions on arbitrary Ethereum contracts. This matches the "theft or unbacked mint/unlock," "duplicate settlement," and "public underpriced work" impact classes: an ordinary user extracts value or triggers unauthorized execution through a public, unprivileged entrypoint (`pallet_xcm::execute`/`send`), with no admin, relayer, or validator involvement — squarely within the accepted impact gate.

### Likelihood Explanation
The path requires only a standard user-facing action: submitting an XCM via `pallet_xcm` containing `WithdrawAsset`/`PayFees`/`AliasOrigin`/`DepositAsset`/`Transact` in the syntax the converter expects (this exact shape is exercised in the repo's own integration tests, e.g. `transact_with_agent_from_asset_hub`, confirming feasibility with normal signed origins). The only gate (`AllowedAliasOrigin`) restricts *which origin* may alias, not what target/value the resulting contract call may use, so likelihood is high wherever any non-privileged origin is permitted to alias (which is the intended common case, e.g. AssetHub users transacting through their own agents).

### Recommendation
- Enforce the (apparently intended but currently unused) `CallContractValueInsufficient` check: require `value` in `ContractCall::V1` to be bounded by assets actually reserved/withdrawn within the same XCM message, not the agent's total held balance.
- Consider adding an allow-list/contains-filter for permissible `target` addresses per agent, analogous to how `AllowedAliasOrigin` restricts origins, so that agents cannot be used as unrestricted call proxies to arbitrary Ethereum contracts.
- Add explicit tests asserting that a `Transact`/`ContractCall::V1` cannot spend more ether than was reserved for that specific message.

### Proof of Concept
1. Attacker controls a normal signed AssetHub account whose location is permitted by `AllowedAliasOrigin` (self-aliasing is the common configuration, as shown by `transact_with_agent_from_asset_hub`).
2. Attacker submits (via `PolkadotXcm::execute`) an XCM: `WithdrawAsset`/`PayFees`/`InitiateTransfer` → `remote_xcm` containing `AliasOrigin(self)`, `DepositAsset`, and `Transact { call: ContractCall::V1{ target: <attacker_contract>, calldata: <arbitrary>, value: <agent's full ether balance>, gas: <limit> }.encode() }`, mirroring the structure in: [8](#0-7) 
3. `EthereumBlobExporter::validate` → `XcmConverter::convert` accepts the payload unmodified (no target/value bound check) and emits `Command::CallContract{target, calldata, gas, value}`.
4. On Ethereum, the Gateway/agent executes the call to `target` with `calldata` and the requested `value` drawn from the agent contract's ether balance, giving the attacker control over arbitrary execution and fund movement — the same "uncontrolled call... with the possibility of transferring ETH" flaw described in the original Pickle Finance report.

Note: I was not able to inspect the Ethereum-side Gateway/agent contract Solidity code (outside this repo's Rust/primitives scope) to confirm whether an additional balance check exists there; this assessment is based solely on the Substrate-side conversion logic, where no such check is present.

### Citations

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/mod.rs (L17-31)
```rust
/// The `XCM::Transact` payload for calling arbitrary smart contracts on Ethereum.
/// On Ethereum, this call will be dispatched by the agent contract acting as a proxy
/// for the XCM origin.
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

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs (L22-48)
```rust
/// Errors that can be thrown to the pattern matching step.
#[derive(PartialEq, Debug)]
pub enum XcmConverterError {
	UnexpectedEndOfXcm,
	EndOfXcmMessageExpected,
	WithdrawAssetExpected,
	DepositAssetExpected,
	NoReserveAssets,
	FilterDoesNotConsumeAllAssets,
	TooManyAssets,
	ZeroAssetTransfer,
	BeneficiaryResolutionFailed,
	AssetResolutionFailed,
	InvalidFeeAsset,
	SetTopicExpected,
	ReserveAssetDepositedExpected,
	InvalidAsset,
	UnexpectedInstruction,
	TooManyCommands,
	AliasOriginExpected,
	InvalidOrigin,
	TransactDecodeFailed,
	TransactParamsDecodeFailed,
	FeeAssetResolutionFailed,
	CallContractValueInsufficient,
	NoCommands,
}
```

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs (L246-256)
```rust
		// Check AliasOrigin.
		let origin_location = match_expression!(self.next()?, AliasOrigin(origin), origin)
			.ok_or(AliasOriginExpected)?;

		// Validate the AliasOrigin using the configured AllowedAliasOrigin filter.
		// This provides a mechanism for the runtime to restrict which origins
		// are permitted to alias, providing defense-in-depth against
		// unprivileged alias attempts.
		ensure!(AllowedAliasOrigin::contains(origin_location), InvalidOrigin);

		let origin = AgentIdOf::convert_location(origin_location).ok_or(InvalidOrigin)?;
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

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/message.rs (L90-98)
```rust
		// Payload for CallContract
		struct CallContractParams {
			// target contract
			address target;
			// Call data
			bytes data;
			// Ether value
			uint256 value;
		}
```

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/message.rs (L182-193)
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

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound.rs (L551-580)
```rust
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
