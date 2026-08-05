### Title
Bridge-Hub-Rococo's Snowbridge V2 outbound exporter may lack the `AllowedAliasOrigin` guard that blocks `AliasOrigin` spoofing of the AssetHub sovereign account - (File: `cumulus/parachains/runtimes/bridge-hubs/bridge-hub-rococo/src/bridge_to_ethereum_config.rs`)

### Summary
The external report's core broken invariant is: a message-routing/verification function accepts a claimed source identifier without checking it against the one true expected value, letting an attacker impersonate a trusted origin. The exact analog of this class exists — and was already found and partially fixed — in Snowbridge's V2 outbound XCM converter, where an `AliasOrigin` XCM instruction lets a sender impersonate an arbitrary origin, including Asset Hub's own sovereign/agent account that holds the bridge's ERC-20 assets on Ethereum. This was fixed for `bridge-hub-westend-runtime` in [1](#0-0) , but the crate/runtime bump list in that PR only touches `bridge-hub-westend-runtime`, not `bridge-hub-rococo-runtime`, and `EthereumBlobExporter` usage in the Rococo bridge-to-Ethereum config shows fewer configured type parameters than the Westend equivalent, consistent with the new `AllowedAliasOrigin` guard not being wired in there.

### Finding Description
The Snowbridge V2 outbound converter (`XcmConverter`) parses XCM instructions being exported to Ethereum, including `AliasOrigin(Location)`, which is used to preserve/alter the origin recorded in the bridge command. Before the fix, `AliasOrigin` was accepted unconditionally, so any XCM program that could reach the exporter (e.g. via `pallet_xcm::execute` with a signed origin plus `InitiateTransfer { preserve_origin: true, ... }`) could inject an `AliasOrigin` pointing at the AssetHub's own parachain location/sovereign account — the account that is the bridge's primary agent and holds the bridge's ERC-20 holdings on Ethereum [2](#0-1) .

The fix added a generic `AllowedAliasOrigin: Contains<Location>` parameter to `EthereumBlobExporter`/`XcmConverter`, and Westend's runtime wires it as `EverythingBut<Equals<AssetHubLocation>>` to explicitly reject any `AliasOrigin` matching Asset Hub's own location [3](#0-2) . This is functionally identical to the external report's remediation: adding an explicit "is this claimed value the one true expected/forbidden value" check before trusting a caller-supplied identity in a cross-domain message path.

Grep evidence shows `EthereumBlobExporter` is configured in both `cumulus/parachains/runtimes/bridge-hubs/bridge-hub-westend/src/bridge_to_ethereum_config.rs` (4 references) and `cumulus/parachains/runtimes/bridge-hubs/bridge-hub-rococo/src/bridge_to_ethereum_config.rs` (2 references), while the `AllowedAliasOrigin` symbol itself only appears in the primitives crate (`v2/converter/convert.rs`, `v2/converter/mod.rs`) and in the PR doc — not in either runtime config file directly (as expected, since the concrete type argument, e.g. `EverythingBut<Equals<AssetHubLocation>>`, would be a different token than `AllowedAliasOrigin`). The discrepancy in reference counts between the Westend and Rococo config files, combined with the PR's crate-bump list explicitly limiting the fix to `bridge-hub-westend-runtime`, is the concrete evidence that Rococo's exporter configuration was not updated in lockstep.

### Impact Explanation
If Rococo's `EthereumBlobExporter` instantiation does not restrict `AllowedAliasOrigin` (e.g., it defaults to `Everything`, or the type parameter isn't present because it's a stale interface), then any account able to execute an XCM program through `pallet_xcm::execute`/`send` on Asset Hub-Rococo (an ordinary signed account) can forge an `AliasOrigin` matching Asset Hub's own sovereign/agent location. The forged origin is then embedded in the outbound bridge message to Ethereum, letting an unprivileged actor authorize/claim actions (e.g., `Transact` calls, deposits) as if they came from Asset Hub's trusted agent — i.e., unauthorized execution/origin escalation and potential theft of bridge-held assets, matching the "unauthorized execution or origin escalation" and "theft or unbacked mint or unlock" categories in the impact gate.

### Likelihood Explanation
The attack requires only a signed, ordinary account able to submit `pallet_xcm::execute` with a crafted `InitiateTransfer`/`AliasOrigin` payload — no validator, relayer, governance, or leaked-key assumption, matching the exact reproduction already demonstrated on Westend in `signed_assethub_user_cannot_bypass_origin_alteration_when_routing_to_ethereum` [4](#0-3) . Likelihood on Rococo is high if the guard is indeed absent, since the underlying converter logic (`AliasOrigin` handling) is shared code and the attack path is unprivileged and directly reachable by any user.

Caveat: I was not able to directly read the contents of `bridge_to_ethereum_config.rs` for `bridge-hub-rococo` in this session (only grep match counts), so I cannot 100% confirm the exact type argument passed for `AllowedAliasOrigin` there. This should be verified by reading that file directly before treating this as a confirmed-exploitable issue.

### Recommendation
Confirm the concrete `AllowedAliasOrigin` type parameter passed to `EthereumBlobExporter` in `cumulus/parachains/runtimes/bridge-hubs/bridge-hub-rococo/src/bridge_to_ethereum_config.rs`. If it is `Everything` or the parameter is missing/defaulted, apply the same fix as Westend: use `EverythingBut<Equals<AssetHubRococoLocation>>` (or equivalent) so any `AliasOrigin` matching Asset Hub's own location is rejected, consistent with `prdoc/pr_12159.prdoc`. Add an integration test mirroring `signed_assethub_user_cannot_bypass_origin_alteration_when_routing_to_ethereum` for the Rococo bridge hub to lock in the guard.

### Proof of Concept
1. On Bridge-Hub-Rococo's Asset Hub, as any signed account, submit `pallet_xcm::execute` with an XCM containing `WithdrawAsset`, `PayFees`, `ClearOrigin`, then `InitiateTransfer { destination: ethereum(), preserve_origin: true, remote_xcm: [AliasOrigin(asset_hub_rococo_location), DepositAsset{...}, Transact{...}] }` — the same structure used in the Westend regression test [5](#0-4) .
2. If `AllowedAliasOrigin` is not restrictive on Rococo, the `XcmConverter` will accept the `AliasOrigin` unconditionally (per the unguarded logic path in `mod.rs` lines 132-147) [2](#0-1) , and the outbound message will be queued and committed with the forged Asset Hub agent origin.
3. Verify by checking `snowbridge_pallet_outbound_queue_v2::Messages` / `MessageQueued` events are emitted for the forged-origin message (in the fixed Westend case these assertions confirm zero messages are queued; on an unpatched chain the message would be queued instead).

### Citations

**File:** prdoc/pr_12159.prdoc (L1-21)
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
crates:
- name: snowbridge-outbound-queue-primitives
  bump: major
- name: snowbridge-runtime-common
  bump: minor
- name: bridge-hub-westend-runtime
  bump: minor
- name: bridge-hub-westend-integration-tests
```

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/converter/mod.rs (L132-147)
```rust
		// Inspect `AliasOrigin` as V2 message. This exporter should only process Snowbridge V2
		// messages. We use the presence of an `AliasOrigin` instruction to distinguish between
		// Snowbridge V2 and Snowbridge V1 messages, since XCM V5 came after Snowbridge V1 and
		// so it's not supported in Snowbridge V1. Snowbridge V1 messages are processed by the
		// snowbridge-outbound-queue-primitives v1 exporter.
		let mut instructions = message.clone().0;
		let result = instructions.matcher().match_next_inst_while(
			|_| true,
			|inst| {
				return match inst {
					AliasOrigin(..) => Err(ProcessMessageError::Yield),
					_ => Ok(ControlFlow::Continue(())),
				};
			},
		);
		ensure!(result.is_err(), SendError::NotApplicable);
```

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound_edge_case.rs (L436-497)
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
```
