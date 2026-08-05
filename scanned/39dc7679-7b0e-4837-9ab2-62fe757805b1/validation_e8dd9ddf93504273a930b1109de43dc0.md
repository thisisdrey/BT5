Based on my investigation, the closest structural analog to the `callTakerTokenAndFillOrder()` pattern (an unprivileged caller directing an arbitrary `target`/`calldata`/`value` call that is executed by a shared, fund-holding proxy) is the `ContractCall::V1` → `Command::CallContract` path in Snowbridge's V2 outbound queue.

### Title
Underspecified authorization for `Command::CallContract` lets any XCM `Transact` drain the Ethereum-side agent's held ether via arbitrary target/calldata - (File: `bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs`)

### Summary
The V2 XCM-to-Ethereum converter accepts an attacker-supplied `ContractCall::V1 { target, calldata, value, gas }` inside an XCM `Transact` and converts it directly into `Command::CallContract`, which the docs state is executed by "the agent contract acting as a proxy for the XCM origin," explicitly allowed to "include ether held by the agent contract" — i.e., not just ether newly bridged in the same message.

### Finding Description
`XcmConverter::convert()` decodes the `Transact` payload as `ContractCall::V1` and pushes `Command::CallContract { target, calldata, gas, value }` with no restriction on `target` or `calldata`, and no check in this function that `value` is bounded by the amount actually deposited/reserved in the current message. [1](#0-0) 
The only gate on this path is `AllowedAliasOrigin::contains(origin_location)` plus `AgentIdOf::convert_location`, which determine which agent the call is attributed to — they do not constrain `target`, `calldata`, or `value`. [2](#0-1) 
The `ContractCall` type itself documents that this is a proxy call using the agent's held balance, not just the transferred amount: "Include ether held by the agent contract." [3](#0-2) 
An error variant `CallContractValueInsufficient` exists in the same error enum, implying an intended value-vs-transfer-amount check, but it is never raised anywhere in `convert()` — the check is deferred to code outside this file (per the test comment "require validation on BH Exporter"), which I could not locate/verify in this scan. [4](#0-3) 
The integration test itself demonstrates exactly the same shape of primitive as the report: a signer picks any `target`, `calldata`, `value`, `gas` and dispatches it via a normal signed `PolkadotXcm::execute`, with the agent contract acting as the fund-holding proxy that will make the arbitrary external call. [5](#0-4) 

### Impact Explanation
If the deferred "value <= transferred amount" check is missing, incomplete, or only checked at delivery/relaying time (not atomically bound to the funds actually moved in that message), an unprivileged account could construct a `Transact` with an inflated `value` and attacker-chosen `target`/`calldata`, causing the Ethereum-side agent contract to send more ether (accumulated from unrelated prior deposits or from other users routed through the same agent) to an arbitrary attacker-controlled contract — a direct theft/unbacked-drain of bridge-held funds, matching the "theft or unbacked mint or unlock" and "public underpriced work that ... stalls bridge processing" impact classes.

### Likelihood Explanation
Reachability is via a standard, unprivileged `pallet_xcm::execute` extrinsic — no relayer, validator, or governance action is required, satisfying the "public entrypoint, unprivileged attacker" requirement. However, I was **not able to verify** in this scan whether the referenced "BH Exporter" validation (`CallContractValueInsufficient`) is actually enforced before message dispatch, nor could I inspect the Ethereum-side Gateway/agent contract logic (out of repo) that ultimately executes `CallContract`. There is also an existing regression test (`signed_assethub_user_cannot_bypass_origin_alteration_when_routing_to_ethereum`) that shows the team has already hardened the *origin-forging* variant of this class of bug, referencing a public postmortem. This reduces confidence that the `value`-bounding gap is actually exploitable rather than enforced elsewhere.

### Recommendation
Enforce, inside `XcmConverter::convert()` itself (not only in a downstream exporter), that `Command::CallContract.value` can never exceed the ether amount actually withdrawn/reserved for the agent in that same message, and consider adding an allow-list of permitted `target` contracts/selectors analogous to the DFlow fix, rather than allowing arbitrary `target`/`calldata` combined with access to the agent's total held balance.

### Proof of Concept
Not independently reproducible from static analysis alone: exploitation depends on code outside this scan's coverage (the BH Exporter's `validate()`/`SendMessage` implementation and the Ethereum Gateway/agent contracts), which I could not retrieve in this pass. The closest reproducible artifact is the existing test `transact_with_agent_from_asset_hub`, which already demonstrates constructing an arbitrary `ContractCall::V1` and successfully queuing it as `Command::CallContract` via a normal signed extrinsic. [6](#0-5) 

**Caveat:** Because the value-bounding check (`CallContractValueInsufficient`) is not present in the file I could inspect and I could not locate/confirm its enforcement in the outbound-queue exporter or in the off-repo Ethereum Gateway contracts, I cannot state with certainty that this is currently exploitable rather than already mitigated elsewhere. This should be treated as a lead requiring confirmation against `bridges/snowbridge/primitives/outbound-queue/src/v2/exporter.rs` and the Snowbridge Ethereum contracts, not a fully proven vulnerability.

### Citations

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs (L44-47)
```rust
	TransactParamsDecodeFailed,
	FeeAssetResolutionFailed,
	CallContractValueInsufficient,
	NoCommands,
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
