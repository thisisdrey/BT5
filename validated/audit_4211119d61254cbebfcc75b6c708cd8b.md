### Title
Unvalidated `ContractCall::V1.target` in Snowbridge outbound-queue-v2 `Transact` command lets any AssetHub user force the Gateway agent to call an arbitrary/non-conforming Ethereum contract, risking loss of the agent's bridged ether - (File: `bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs`)

### Summary
This is the direct on-chain analog of the Axelar report: a cross-chain "execute a call on `destinationAddress`" primitive that does not hardcode or whitelist the destination, and where the comment/spec around what that address is supposed to be (and what it must implement) is not enforced by any guard. In Snowbridge V2, the `XcmConverter::convert()` function decodes a user-supplied `ContractCall::V1 { target, calldata, gas, value }` straight out of an XCM `Transact` instruction and turns it into `Command::CallContract { target, calldata, gas, value }`, with **no check at all on `target`** (no whitelist, no restriction to known Gateway/agent-compatible contracts) before it is queued for on-chain execution on Ethereum by the Gateway acting on behalf of the user's agent.

### Finding Description
`XcmConverter::convert()` in [1](#0-0)  extracts an optional `Transact` instruction from the AssetHub-originated XCM, decodes it as `ContractCall::V1`, and pushes `Command::CallContract { target, calldata, gas, value }` unconditionally:

```rust
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

The `target` field is defined as an arbitrary 20-byte address in [2](#0-1)  with the comment `/// Target contract address` — no further constraint. This is functionally identical to the Axelar `destinationAddress` parameter: an unrestricted external-chain address that the bridge's agent/proxy contract is trusted to call, carrying real value (`value: u128` — "Include ether held by agent contract", per `Command::CallContract` doc at [3](#0-2) ).

The only guard present in `convert()` is on the XCM-side `AliasOrigin` (via `AllowedAliasOrigin::contains`, [4](#0-3) ), which restricts *who* can originate/alias the message, not *where* the resulting Ethereum-side contract call can be directed. Nothing in this file, `message.rs`, or `exporter.rs` (checked — it only gates on operating-mode pause, [5](#0-4) ) validates `target` against the Gateway address, the caller's own agent address, or any allow-list of contracts known to correctly implement a receiving interface. The existing integration test comment itself flags an *unrelated* value-vs-transfer-amount check ("value should be less than the transfer amount, require validation on BH Exporter", [6](#0-5) ) but confirms no analog exists for `target` itself.

### Impact Explanation
Any signed AssetHub account can construct an `InitiateTransfer`/`Transact` XCM (as demonstrated by the repository's own `transact_with_agent_from_asset_hub` test, [7](#0-6) ) that embeds an arbitrary `target` address. Once relayed and executed on Ethereum by the Gateway/agent, if `target` is not a contract implementing the expected receiving interface (or reverts, self-destructs unexpectedly, or is an EOA/non-existent address), the `value` (ether held by the user's agent) forwarded in the call can be irrecoverably lost or stranded — exactly the "transferred tokens will be lost" impact described in the Axelar report. Because the agent is the entity whose funds are spent, and the target is fully attacker-chosen with no whitelist, this is a fund-loss/fund-lock vector reachable by an unprivileged, ordinary AssetHub user, matching the "theft or unbacked mint or unlock" / "permanent user-fund... lock" impact categories.

### Likelihood Explanation
High reachability: the path is a normal, unprivileged `pallet_xcm::execute`/`transfer_assets_using_type_and_then` call from any signed AssetHub account, requiring no governance, relayer, or validator collusion — only knowledge of how to construct the `InitiateTransfer`/`Transact`/`ContractCall::V1` payload, which the repository's own test suite already demonstrates end-to-end. The only barrier is the `AllowedAliasOrigin` check, which gates *origin* aliasing, not the *destination contract* of the resulting call, so it does not block this specific misuse.

### Recommendation
Mirror the Axelar report's recommendation: hardcode or whitelist permissible `target` addresses for `Command::CallContract` (e.g., restrict to the Gateway's own known agent-proxy pattern or an explicit allow-list maintained by governance), and/or require calls to be self-targeted to the invoking agent's controller contract only. At minimum, add an explicit runtime-enforced check in `XcmConverter::convert()` (or in `Command`/`Message` validation prior to `SendMessage::validate`) that rejects `target` values outside an approved set, and document unambiguously (as recommended by Spearbit for Axelar) exactly which Ethereum-side contract `target` is expected to be and what interface it must implement.

### Proof of Concept
1. As any signed AssetHub account, build an XCM identical in structure to `transact_with_agent_from_asset_hub` ( [7](#0-6) ), but set `ContractCall::V1.target` to an address known to have no code (or a contract without a `receive`/fallback that safely handles unsolicited value/calldata).
2. Submit via `PolkadotXcm::execute` on AssetHub; the message passes `AllowedAliasOrigin` (using the caller's own legitimate alias) and is queued to Ethereum unmodified, per the unconditional push in `convert()` (lines 294-305 above).
3. On delivery to the Gateway on Ethereum, the `CallContract` command executes against the attacker-chosen `target`, forwarding `value` ether from the agent; because `target` implements no compatible logic (or reverts/consumes the call unexpectedly), the ether is lost or becomes unrecoverable to the legitimate agent owner — with no on-chain guard in this repository preventing that outcome.

### Citations

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

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/mod.rs (L17-32)
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
}
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

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/exporter.rs (L19-27)
```rust
	fn validate(
		destination: &mut Option<Location>,
		message: &mut Option<Xcm<()>>,
	) -> SendResult<Self::Ticket> {
		match PausedQuery::is_paused() {
			true => Err(SendError::NotApplicable),
			false => InnerExporter::validate(destination, message),
		}
	}
```

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound.rs (L516-580)
```rust
#[test]
fn transact_with_agent_from_asset_hub() {
	let weth_asset_location: Location = weth_location();

	fund_on_bh();

	register_assets_on_ah();

	fund_on_ah();

	AssetHubWestend::execute_with(|| {
		type RuntimeOrigin = <AssetHubWestend as Chain>::RuntimeOrigin;

		let local_fee_asset =
			Asset { id: AssetId(Location::parent()), fun: Fungible(LOCAL_FEE_AMOUNT_IN_DOT) };

		let remote_fee_asset =
			Asset { id: AssetId(ethereum()), fun: Fungible(REMOTE_FEE_AMOUNT_IN_ETHER) };

		let reserve_asset =
			Asset { id: AssetId(weth_asset_location.clone()), fun: Fungible(TOKEN_AMOUNT) };

		let assets = vec![reserve_asset.clone(), local_fee_asset.clone(), remote_fee_asset.clone()];

		let beneficiary =
			Location::new(0, [AccountKey20 { network: None, key: AGENT_ADDRESS.into() }]);

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
