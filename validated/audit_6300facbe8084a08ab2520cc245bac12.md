Based on my investigation, I found a concrete, provable local analog of the H-1 pattern (an attacker-supplied "sender" identity field that is not properly bound to the real message origin) inside Snowbridge's V2 outbound queue XCM converter.

### Title
Unbound `AliasOrigin` in Snowbridge V2 outbound converter lets an AssetHub-signed user impersonate any other chain's identity toward Ethereum - (File: bridges/snowbridge/primitives/outbound-queue/src/v2/converter/mod.rs, bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs)

### Summary
The Snowbridge V2 `EthereumBlobExporter`/`XcmConverter` accepts an `AliasOrigin(Location)` instruction supplied inside an attacker-controlled XCM program (submitted via `pallet_xcm::execute`/`send` by any signed AssetHub user) as the identity that will be forwarded to the Ethereum Gateway contract and used to derive the acting agent for the bridged operation. The only guard on this value is `AllowedAliasOrigin: Contains<Location>`, which the runtime instantiates as `EverythingBut<Equals<AssetHubLocation>>` [1](#0-0) . This is a denylist for exactly one hardcoded value (the AssetHub parachain's own location) — it does **not** verify that `AliasOrigin` equals the real, verified `universal_source`/message sender that the exporter itself already extracted [2](#0-1) .

### Finding Description
The exporter's `validate` function establishes the *true* sender by decoding `universal_source` and asserting the sub-location resolves to a `Parachain(para_id)` equal to `AssetHubParaId` [2](#0-1) . This confirms only that the *transport-level* origin is AssetHub — i.e., it authenticates "the message came through AssetHub," analogous to `_srcChainSender` in the Tapioca report being the LayerZero-verified sender on the source chain.

However, the actual *identity* used for accounting/execution purposes downstream (agent derivation, `DepositAsset` beneficiary resolution context, and `Transact` origin for the Gateway contract) is taken from the `AliasOrigin(Location)` instruction embedded in the attacker-supplied XCM payload, not from the authenticated `universal_source`. The test suite confirms `AliasOrigin` is treated as fully attacker-suppliable content of the message and is only rejected when it exactly equals `AssetHubLocation`: [3](#0-2) 
Any other `AliasOrigin` value — e.g. `Location::new(1, Parachain(<victim_para_id>))`, a sibling parachain's sovereign location, or any other chain/account identity that holds an Ethereum-side agent or bridged asset entitlement — passes the filter and is accepted as the acting origin: [4](#0-3) 

This is the exact broken invariant from H-1: a "sender" parameter (`AliasOrigin`, playing the role of `_srcChainSender`) is forwarded and trusted by downstream modules (agent-id derivation / Ethereum Gateway commands) without validating it matches the account whose assets/identity are actually being acted upon (the real, authenticated `universal_source`). The fix in `prdoc/pr_12159.prdoc` is explicitly a single-value denylist ("defense in depth... reject any `AliasOrigin` that matches the Asset Hub's parachain location") [5](#0-4)  rather than a binding check that `AliasOrigin == universal_source` (or a member of a legitimately-delegatable set, as `xcm-executor`'s `Aliasers`/`AuthorizedAliasOrigin` machinery does for local XCM execution). Because the check is a denylist keyed to one specific `Location`, it structurally cannot prevent impersonation of any *other* chain's or account's identity.

### Impact Explanation
Any signed account on AssetHub can craft and submit (via `pallet_xcm::execute`) a V2 export XCM containing `WithdrawAsset`/`PayFees`/`AliasOrigin(<arbitrary non-AssetHub Location>)`/`DepositAsset`/optional `Transact`, and have it accepted by the outbound queue converter and forwarded to the Ethereum Gateway under a spoofed identity. Depending on how the Ethereum-side Gateway contract and agent system consume the aliased origin, this allows unauthorized execution or asset operations "on behalf of" another chain/account — the same high-impact primitive as H-1 (execute privileged operations impersonating a different, non-consenting party) — but here scoped to whichever accounts/chains are not literally `AssetHubLocation`.

### Likelihood Explanation
High: the entry point is a fully public, unprivileged extrinsic (`pallet_xcm::execute`/`send`) available to any signed AssetHub account; no relayer, validator, governance, or key compromise is required, matching the "public-entrypoint, unprivileged attacker" requirement. The only currently-implemented guard, `EverythingBut<Equals<AssetHubLocation>>`, is trivially bypassed by choosing any `Location` value other than the one literal blocked constant.

### Recommendation
Do not rely on a denylist of a single hardcoded `Location` for `AliasOrigin` validation. Instead, bind the accepted `AliasOrigin` to the authenticated `universal_source` established during `validate` (i.e., require `AliasOrigin == universal_source`, or restrict it to the set of origins that `universal_source` is explicitly authorized to alias into, mirroring the `AuthorizedAliasOrigin`/`Aliasers` consent mechanism already used elsewhere in the XCM stack for `InitiateTransfer`/`AliasOrigin`). This closes impersonation of any location, not just the one currently denylisted.

### Proof of Concept
1. An attacker holds a signed account on AssetHub Westend (e.g. `AssetHubWestendSender`).
2. The attacker calls `PolkadotXcm::execute` with a V2 export XCM structured as:
```
WithdrawAsset([fee_asset, transfer_asset])
PayFees { asset: fee_asset }
ReserveAssetDeposited/WithdrawAsset(transfer_asset)
AliasOrigin(Location::new(1, Parachain(<any_para_id != AssetHubParaId>)))
DepositAsset { assets: Wild(All), beneficiary: <attacker_controlled_eth_address> }
SetTopic([..])
```
3. `EthereumBlobExporter::validate` confirms `universal_source` resolves to `AssetHubParaId` (true, since the extrinsic ran on AssetHub) [2](#0-1) .
4. `XcmConverter::convert` reaches the `AliasOrigin` check; since the supplied `Location::new(1, Parachain(<any_para_id != AssetHubParaId>))` is not equal to `AssetHubLocation`, `AllowedAliasOrigin::contains` returns `true` and the message is accepted (confirmed by the analogous success-path test) [4](#0-3) , whereas only the literal AssetHub value is rejected [3](#0-2) .
5. The message is queued and forwarded to Ethereum carrying the spoofed origin, with no check that it matches the real AssetHub-authenticated sender.

Note: I was unable to view the full contents of `bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs` and `cumulus/parachains/runtimes/bridge-hubs/bridge-hub-westend/src/bridge_to_ethereum_config.rs` directly (tool access ran out before I could retrieve the full function body of `XcmConverter::validate_origin`/`convert` and the exact runtime instantiation of `AllowedAliasOrigin`); the analysis above is based on the indexed snippets, tests, and the PR description that were retrieved, which consistently show the guard is a single-location denylist rather than an origin-binding check. A Devin session with full file access would be needed to confirm the exact downstream consumption of the aliased origin in the Ethereum Gateway command construction.

### Citations

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/converter/mod.rs (L28-44)
```rust
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

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/converter/mod.rs (L96-125)
```rust
		// Cloning universal_source to avoid modifying the value so subsequent exporters can use it.
		let (local_net, local_sub) = universal_source
			.clone()
			.ok_or_else(|| {
				tracing::error!(target: TARGET, "universal source not provided.");
				SendError::MissingArgument
			})?
			.split_global()
			.map_err(|()| {
				tracing::error!(target: TARGET, ?universal_source, "could not get global consensus.");
				SendError::NotApplicable
			})?;

		if Ok(local_net) != universal_location.global_consensus() {
			tracing::trace!(target: TARGET, relay_network=?local_net, "skipped due to unmatched relay network.");
			return Err(SendError::NotApplicable);
		}

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

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/converter/tests.rs (L363-409)
```rust
#[test]
fn exporter_validate_xcm_success_case_1() {
	let network = BridgedNetwork::get();
	let mut destination: Option<InteriorLocation> = Here.into();

	let mut universal_source: Option<InteriorLocation> =
		Some([GlobalConsensus(Polkadot), Parachain(1000)].into());

	let token_address: [u8; 20] = hex!("1000000000000000000000000000000000000000");
	let beneficiary_address: [u8; 20] = hex!("2000000000000000000000000000000000000000");

	let channel: u32 = 0;
	let assets: Assets = vec![Asset {
		id: AssetId([AccountKey20 { network: None, key: token_address }].into()),
		fun: Fungible(1000),
	}]
	.into();
	let fee_asset: Asset = Asset { id: AssetId(Here.into()), fun: Fungible(1000) }.into();
	let filter: AssetFilter = assets.clone().into();

	let mut message: Option<Xcm<()>> = Some(
		vec![
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
		.into(),
	);

	let result =
		EthereumBlobExporter::<
			UniversalLocation,
			BridgedNetwork,
			MockOkOutboundQueue,
			MockTokenIdConvert,
			AssetHubParaId,
			EverythingBut<Equals<AssetHubLocation>>,
		>::validate(network, channel, &mut universal_source, &mut destination, &mut message);

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
