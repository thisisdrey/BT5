## Finding [1](#0-0) 

### Title
V2 Snowbridge outbound converter binds `Message.origin` to an attacker-supplied `AliasOrigin` location instead of the authenticated sender, letting any AssetHub XCM caller impersonate another chain's agent - (File: `bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs`)

### Summary
Snowbridge V2's outbound queue converts a local XCM into a `Command`/`Message` destined for the Ethereum Gateway. The `Message.origin` (which selects which on-chain "agent" — and therefore whose held Ether/assets — the Gateway acts as) is derived purely from an `AliasOrigin(origin_location)` instruction embedded in the *payload* of the XCM, not from the cryptographically authenticated sender (the parachain that actually dispatched the message via HRMP/`ExportMessage`). The only guard against forgery, added in a prior fix, is a single denylist entry (`AssetHubLocation`), leaving every other location free to be claimed.

### Finding Description
In `EthereumBlobExporter::validate` for v2 (`bridges/snowbridge/primitives/outbound-queue/src/v2/converter/mod.rs:69-164`), the code establishes the real caller's identity (`para_id` from `universal_source`) and enforces it must be AssetHub: [2](#0-1) 

But unlike the v1 exporter, this authenticated `para_id`/`agent_id` is **never passed into** the v2 `XcmConverter` — no `source_location`/`agent_id` argument is threaded through: [3](#0-2) 

Instead, `XcmConverter::convert` (`bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs:246-256`) pulls the origin directly out of the XCM payload's own `AliasOrigin` instruction — data fully controlled by whoever constructs the XCM — and only checks it against a single denylisted location: [4](#0-3) 

This resulting `origin` (an agent id) is placed directly into the outbound `Message`: [5](#0-4) 

The `AllowedAliasOrigin` filter used in the runtime is `EverythingBut<Equals<AssetHubLocation>>` (per `prdoc/pr_12159.prdoc`), i.e. it blocks exactly one location (AssetHub's own sovereign location) and allows literally every other `Location`, including sibling-parachain sovereign accounts or BridgeHub's own governance agent (`Location::here()`), to be asserted as the message's origin: [6](#0-5) 

The existing regression test only proves that impersonating AssetHub itself is blocked; it does not, and cannot, prevent impersonation of any other registered agent: [7](#0-6) 

This is the direct structural analog of the M-10 report: `cross_chain_erc20_settlement` never validated that `to_handler` belonged to the caller/was whitelisted for that message; here, the converter never validates that the `AliasOrigin` claimed in the payload actually corresponds to the authenticated sender that produced the XCM (or is even a registered/legitimate agent) — it only excludes one specific value.

### Impact Explanation
The resulting `Message.origin` selects the Ethereum-side "agent" contract that the Gateway executes commands as. Commands built downstream of this origin include `CallContract { target, calldata, gas, value }`, where the comment states value can "include ether held by agent contract" — i.e. funds held by that specific agent. A signed, unprivileged AssetHub account can therefore submit an XCM (as demonstrated by the existing PoC test `signed_assethub_user_cannot_bypass_origin_alteration_when_routing_to_ethereum`) whose `remote_xcm` sets `AliasOrigin` to any location other than AssetHub's own — e.g. a sibling parachain's sovereign account or BridgeHub's governance agent — to have the Gateway execute contract calls (potentially draining Ether or invoking arbitrary calldata) "as" that other party's agent. This is unauthorized-origin escalation and theft/loss of bridge-held funds without requiring a malicious relayer, validator, or admin — matching the Polkadot SDK Impact Gate's criteria for "unauthorized execution or origin escalation" and "theft or unbacked mint or unlock."

### Likelihood Explanation
The attack path requires only a signed AssetHub account able to call `pallet_xcm::execute` (already demonstrated feasible in-repo test scaffolding) and knowledge of a target agent's `Location` (many of which are derivable/predictable, e.g. `Location::here()` for BridgeHub, or `Location::new(1,[Parachain(id)])` for any sibling parachain). No governance, relayer, or validator compromise is needed — this is a public entrypoint reachable by any unprivileged user.

### Recommendation
Do not derive `Message.origin` solely from an attacker-supplied `AliasOrigin` value validated against a denylist. Instead, bind the origin to the authenticated sender established during `validate()` (the `agent_id` computed from `source_location`/`para_id`), as v1 does, and only allow `AliasOrigin` to further restrict/refine within that sender's own authority (e.g. verifying it matches or is a sub-location of the true sender), replacing the `EverythingBut<Equals<AssetHubLocation>>` denylist with an allowlist tied to the actual authenticated origin.

### Proof of Concept
1. As any signed AssetHub account, call `PolkadotXcm::execute` with an XCM containing `InitiateTransfer { remote_xcm: [ ..., AliasOrigin(Location::new(1, [Parachain(<victim_para_id>)])), DepositAsset{...}, Transact{ call: CallContract{ target: <attacker_addr>, value: <agent_balance>, ...} }, SetTopic(..) ] }`, following the same pattern in `signed_assethub_user_cannot_bypass_origin_alteration_when_routing_to_ethereum` but substituting a sibling parachain location instead of `forged_assethub_origin`.
2. Because `AllowedAliasOrigin = EverythingBut<Equals<AssetHubLocation>>`, the `ensure!(AllowedAliasOrigin::contains(origin_location), InvalidOrigin)` check in `convert.rs:254` passes.
3. `AgentIdOf::convert_location(origin_location)` derives the victim parachain's real agent id, and the resulting `Message.origin` set to that agent id is queued and later committed for Ethereum execution as that agent.
4. On Ethereum, the Gateway executes the `CallContract` command using the victim agent's held Ether/authority, as authenticated bridge traffic — without the victim parachain ever authorizing the action.

### Citations

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/converter/mod.rs (L46-68)
```rust
impl<
		UniversalLocation,
		EthereumNetwork,
		OutboundQueue,
		ConvertAssetId,
		AssetHubParaId,
		AllowedAliasOrigin,
	> ExportXcm
	for EthereumBlobExporter<
		UniversalLocation,
		EthereumNetwork,
		OutboundQueue,
		ConvertAssetId,
		AssetHubParaId,
		AllowedAliasOrigin,
	>
where
	UniversalLocation: Get<InteriorLocation>,
	EthereumNetwork: Get<NetworkId>,
	OutboundQueue: SendMessage,
	ConvertAssetId: MaybeConvert<TokenId, Location>,
	AssetHubParaId: Get<ParaId>,
	AllowedAliasOrigin: Contains<Location>,
```

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/converter/mod.rs (L114-125)
```rust
		let para_id = match local_sub.as_slice() {
			[Parachain(para_id)] => *para_id,
			_ => {
				tracing::error!(target: TARGET, universal_source=?local_sub, "could not get parachain id.");
				return Err(SendError::NotApplicable);
			},
		};

		if ParaId::from(para_id) != AssetHubParaId::get() {
			tracing::error!(target: TARGET, ?para_id, "is not from asset hub.");
			return Err(SendError::NotApplicable);
		}
```

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/converter/mod.rs (L149-151)
```rust
		let mut converter =
			XcmConverter::<ConvertAssetId, (), AllowedAliasOrigin>::new(&message, expected_network);
		let message = converter.convert().map_err(|err| {
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

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs (L310-317)
```rust
		let topic_id = match_expression!(self.next()?, SetTopic(id), id).ok_or(SetTopicExpected)?;

		let message = Message {
			id: (*topic_id).into(),
			origin,
			fee: fee_amount,
			commands: BoundedVec::try_from(commands).map_err(|_| TooManyCommands)?,
		};
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
