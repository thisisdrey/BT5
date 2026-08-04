## Title
Unauthenticated `AliasOrigin` forgery lets any XCM sender impersonate arbitrary sibling chains/accounts in Snowbridge outbound messages - (File: `bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs`)

### Summary
`XcmConverter::convert` derives the Ethereum-side `Message.origin` (an `AgentId`) purely from the `AliasOrigin(origin_location)` instruction embedded in the *inner* XCM program submitted for export, gated only by a `Contains<Location>` filter, `AllowedAliasOrigin`. In this repo the filter is instantiated as `EverythingBut<Equals<AssetHubLocation>>`, i.e. it blocks exactly one hard-coded location (Asset Hub's own) and allows every other location to be asserted as the origin. [1](#0-0) 

This is the direct structural analog of the MarginRouter bug: the router (here, `XcmConverter`) does perform *an* entry check (`registerTradeAndBorrow` ~ `AllowedAliasOrigin::contains`), but the check only guards against one specific, pre-selected value rather than verifying that the asserted identity actually matches the true caller/route. Any other "pair"/origin the attacker chooses to construct passes straight through.

### Finding Description
`XcmConverter::convert` is reached via `EthereumBlobExporter`/`ExportMessage`, which is invoked when the XCM executor processes an `ExportMessage` instruction — something a signed, unprivileged account can trigger through the normal `pallet_xcm::execute` extrinsic (as demonstrated by the emulated regression test that exercises exactly this flow). [2](#0-1) 

Inside the converter, the code trusts a literal `AliasOrigin(Location)` value carried in the message payload as the authoritative origin for the resulting `Message`, converting it into an `AgentId` that becomes the caller identity delivered to the Ethereum Gateway contract: [1](#0-0) 

The only safeguard, per the PR that introduced it, is a filter that rejects a *single* location (Asset Hub's): [3](#0-2) [4](#0-3) 

Test evidence confirms the filter's design intent is narrowly scoped: it only asserts that `AliasOrigin(AssetHubLocation)` is rejected, while any *other* forged location (e.g. `Location::new(1, [GlobalConsensus(Polkadot), Parachain(1000)])`) is accepted and converted successfully: [5](#0-4) [6](#0-5) 

Nothing in `convert()` ties `origin_location` back to the actual sender/origin that triggered the `ExportMessage` (e.g. the sovereign account of the parachain that actually submitted the XCM, or the interior location it is permitted to alias to under the chain's own `Aliasers` configuration). The check is a denylist of one specific value, not an allowlist tied to the real caller — structurally identical to MarginSwap's flaw where `registerTradeAndBorrow`'s `isMarginTrader(msg.sender)` check trivially passes because `msg.sender` is always the router itself; here `AllowedAliasOrigin::contains` trivially passes for any location that isn't the one hard-coded exception.

### Impact Explanation
`Message.origin` (the `AgentId`) determines which Ethereum-side agent contract executes `CallContract`/`UnlockNativeToken`/`MintForeignToken` commands on the Gateway. An attacker who can submit an `ExportMessage`-bearing XCM (any account able to call `pallet_xcm::execute` with assets to pay local Ethereum-side fees) can set `AliasOrigin` to any sibling parachain's or account's location other than the one excluded value, causing the Gateway to:
- Execute `CallContract` (arbitrary calldata/target) as if issued by that impersonated chain's agent, or
- Trigger `UnlockNativeToken`/`MintForeignToken` attributed to that forged origin.

This is unauthorized origin escalation / cross-chain identity spoofing with direct fund and control impact on the Ethereum side (theft or unbacked unlock/mint attributed to a victim chain's agent), matching the "Public underpriced work / unauthorized execution or origin escalation / theft or unbacked mint or unlock" impact categories in scope.

### Likelihood Explanation
High for any location other than Asset Hub's own. The precondition is only that the attacker control a signed account able to submit `pallet_xcm::execute` with an `ExportMessage` to Ethereum (a normal, unprivileged capability), and craft the inner XCM's `AliasOrigin` value — no relayer, validator, governance, or key compromise is required. The existing test suite itself demonstrates the negative-only nature of the guard by only asserting rejection of the one excluded value, and by construction, all other values pass.

### Recommendation
Do not let `AliasOrigin` inside the exported XCM by itself determine the trusted Ethereum-side identity. Either:
- Derive `Message.origin` from the XCM executor's actual verified origin register at the point `ExportMessage` executes (not from a value embedded and self-asserted inside the exported program), or
- Replace the denylist (`EverythingBut<Equals<AssetHubLocation>>`) with an allowlist that ties the permitted `AliasOrigin` value to the location that is cryptographically/structurally proven to be the true origin of the export (e.g., only allow aliasing to interior locations of the actual sending origin, mirroring `DescendOrigin`/`AliasChildLocation` semantics), and reject anything else, for every location, not just one hard-coded chain.

### Proof of Concept
1. As any signed account on a chain capable of submitting XCM (e.g. via `pallet_xcm::execute`), construct an XCM: `WithdrawAsset(fee) -> PayFees -> WithdrawAsset/ReserveAssetDeposited(assets) -> AliasOrigin(Location::new(1,[Parachain(<victim_para_id>)])) -> DepositAsset -> Transact(ContractCall::V1{target, calldata,...}) -> SetTopic`.
2. Route it through `ExportMessage` to Ethereum via the configured `EthereumBlobExporter`.
3. `XcmConverter::convert` calls `AllowedAliasOrigin::contains(Location::new(1,[Parachain(<victim_para_id>)]))`, which returns `true` because the filter only excludes the exact `AssetHubLocation`, as shown by the existing test `xcm_converter_convert_success` at [7](#0-6)  using an unrelated `Parachain(1000)` alias origin that is accepted.
4. The resulting `Message.origin` is the `AgentId` derived from `<victim_para_id>`'s location, causing the Ethereum Gateway to execute the attacker's `CallContract`/asset commands as that victim agent.

I was not able to independently verify the runtime-level `Aliasers`/XCM executor configuration on BridgeHub within the time available (whether `AliasOrigin` itself is further restricted before reaching the exporter for non-Asset-Hub locations); this would need to be checked in the BridgeHub runtime's XCM config to determine whether the outer XCM executor's own alias-authorization rules provide an independent stop, which the converter-level defense-in-depth explicitly assumes may fail ("defense-in-depth against upstream XCM regressions").

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

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound_edge_case.rs (L436-498)
```rust
#[test]
pub fn signed_assethub_user_cannot_bypass_origin_alteration_when_routing_to_ethereum() {
	fund_on_bh();
	fund_on_ah();

	let forged_assethub_origin = Location::new(1, [Parachain(AssetHubWestend::para_id().into())]);
	let expected_assethub_agent = AgentIdOf::convert_location(&forged_assethub_origin).unwrap();
	assert_eq!(
		expected_assethub_agent,
		hex!("81c5ab2571199e3188135178f3c2c8e2d268be1313d029b30f534fa579b69b79").into()
	);

	AssetHubWestend::execute_with(|| {
		type RuntimeOrigin = <AssetHubWestend as Chain>::RuntimeOrigin;

		let local_fee_asset =
			Asset { id: AssetId(Location::parent()), fun: Fungible(LOCAL_FEE_AMOUNT_IN_DOT) };

		let remote_fee_asset =
			Asset { id: AssetId(ethereum()), fun: Fungible(REMOTE_FEE_AMOUNT_IN_ETHER) };

		let arbitrary_agent_call = ContractCall::V1 {
			target: ETHEREUM_DESTINATION_ADDRESS,
			calldata: vec![0xde, 0xad, 0xbe, 0xef],
			value: 0,
			gas: 100_000,
		};

		let assets = vec![local_fee_asset.clone(), remote_fee_asset.clone()];
		let forged_xcm = Xcm(vec![
			WithdrawAsset(assets.into()),
			PayFees { asset: local_fee_asset },
			// Clear the origin register to None. Under the logic flaw in the XCM executor's
			// InitiateTransfer implementation (with preserve_origin: true), this causes the
			// executor to export the message without prepending any origin-altering instructions.
			// Details: https://forum.polkadot.network/t/postmortem-xcm-initiatetransfer-origin-leak/17357
			ClearOrigin,
			InitiateTransfer {
				destination: ethereum(),
				remote_fees: Some(AssetTransferFilter::ReserveWithdraw(Definite(
					remote_fee_asset.into(),
				))),
				preserve_origin: true,
				assets: BoundedVec::truncate_from(vec![]),
				remote_xcm: Xcm(vec![
					AliasOrigin(forged_assethub_origin),
					DepositAsset { assets: Wild(AllCounted(0)), beneficiary: beneficiary() },
					Transact {
						origin_kind: OriginKind::Xcm,
						call: arbitrary_agent_call.encode().into(),
						fallback_max_weight: None,
					},
					SetTopic([9u8; 32]),
				]),
			},
		]);

		assert_ok!(<AssetHubWestend as AssetHubWestendPallet>::PolkadotXcm::execute(
			RuntimeOrigin::signed(AssetHubWestendSender::get()),
			bx!(VersionedXcm::from(forged_xcm)),
			Weight::from(EXECUTION_WEIGHT),
		));
	});
```

**File:** prdoc/pr_12159.prdoc (L1-13)
```text
title: 'Snowbridge: blocks an origin-spoofing attack vector in the V2 outbound queue converter'
doc:
- audience: Runtime Dev
  description: |-
    Adds a validation check in the V2 XCM converter to reject AliasOrigin instructions
    that attempt to forge the Asset Hub sovereign account origin. This acts as a
    "defense in depth" against upstream XCM regressions, protecting the bridge's primary
    agent account (derived from the Asset Hub Root location) which holds ERC20 assets.

    The `EthereumBlobExporter` and `XcmConverter` now accept a generic
    `AllowedAliasOrigin: Contains<Location>` type parameter. Runtimes pass
    `EverythingBut<Equals<AssetHubLocation>>` to reject any `AliasOrigin` that
    matches the Asset Hub's parachain location.
```

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/converter/mod.rs (L26-44)
```rust
/// Used to process ExportMessages where the destination is Ethereum. It takes an ExportMessage
/// and converts it into a simpler message that the Ethereum gateway contract can understand.
pub struct EthereumBlobExporter<
	UniversalLocation,
	EthereumNetwork,
	OutboundQueue,
	ConvertAssetId,
	AssetHubParaId,
	AllowedAliasOrigin,
>(
	PhantomData<(
		UniversalLocation,
		EthereumNetwork,
		OutboundQueue,
		ConvertAssetId,
		AssetHubParaId,
		AllowedAliasOrigin,
	)>,
);
```

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/converter/tests.rs (L531-565)
```rust
#[test]
fn xcm_converter_convert_success() {
	let network = BridgedNetwork::get();

	let token_address: [u8; 20] = hex!("1000000000000000000000000000000000000000");
	let beneficiary_address: [u8; 20] = hex!("2000000000000000000000000000000000000000");

	let assets: Assets = vec![Asset {
		id: AssetId([AccountKey20 { network: None, key: token_address }].into()),
		fun: Fungible(1000),
	}]
	.into();
	let filter: AssetFilter = assets.clone().into();

	let fee_asset: Asset = Asset { id: AssetId(Here.into()), fun: Fungible(1000) }.into();

	let message: Xcm<()> = vec![
		WithdrawAsset(fee_asset.clone().into()),
		PayFees { asset: fee_asset },
		WithdrawAsset(assets.clone()),
		AliasOrigin(Location::new(1, [GlobalConsensus(Polkadot), Parachain(1000)])),
		DepositAsset {
			assets: filter,
			beneficiary: AccountKey20 { network: None, key: beneficiary_address }.into(),
		},
		SetTopic([0; 32]),
	]
	.into();
	let mut converter =
		XcmConverter::<MockTokenIdConvert, (), EverythingBut<Equals<AssetHubLocation>>>::new(
			&message, network,
		);
	let result = converter.convert();
	assert!(result.is_ok());
}
```

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/converter/tests.rs (L567-600)
```rust
#[test]
fn xcm_converter_convert_with_assethub_alias_origin_yields_invalid_origin() {
	let network = BridgedNetwork::get();

	let token_address: [u8; 20] = hex!("1000000000000000000000000000000000000000");
	let beneficiary_address: [u8; 20] = hex!("2000000000000000000000000000000000000000");

	let assets: Assets = vec![Asset {
		id: AssetId([AccountKey20 { network: None, key: token_address }].into()),
		fun: Fungible(1000),
	}]
	.into();
	let filter: AssetFilter = assets.clone().into();
	let fee_asset: Asset = Asset { id: AssetId(Here.into()), fun: Fungible(1000) }.into();

	let message: Xcm<()> = vec![
		WithdrawAsset(fee_asset.clone().into()),
		PayFees { asset: fee_asset },
		WithdrawAsset(assets.clone()),
		AliasOrigin(AssetHubLocation::get()),
		DepositAsset {
			assets: filter,
			beneficiary: AccountKey20 { network: None, key: beneficiary_address }.into(),
		},
		SetTopic([0; 32]),
	]
	.into();
	let mut converter =
		XcmConverter::<MockTokenIdConvert, (), EverythingBut<Equals<AssetHubLocation>>>::new(
			&message, network,
		);
	let result = converter.convert();
	assert_eq!(result.err(), Some(XcmConverterError::InvalidOrigin));
}
```
