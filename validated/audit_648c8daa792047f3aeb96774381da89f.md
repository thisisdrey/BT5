## Analysis: Incomplete allowlist in Snowbridge V2 outbound `AliasOrigin` sanitization

The report's core defect — a value that determines *who controls funds/authority* is taken from untrusted deserialized/user-supplied input and only partially/naively validated — maps onto a concrete, still-present gap in the Snowbridge V2 outbound queue's XCM converter.

### Title
Incomplete `AliasOrigin` denylist in Snowbridge V2 outbound converter allows origin-spoofing of any non-AssetHub agent - (File: `bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs`)

### Summary
`XcmConverter::convert()` extracts an `AliasOrigin(Location)` instruction from a user-supplied remote XCM program and uses it, via `AgentIdOf::convert_location`, as the authoritative "origin" of the outbound Snowbridge message that is delivered to the Ethereum Gateway contract. This origin selects which on-chain agent contract executes any embedded `Transact` command. The only guard against forgery is `ensure!(AllowedAliasOrigin::contains(origin_location), InvalidOrigin)` where `AllowedAliasOrigin = EverythingBut<Equals<AssetHubLocation>>` [1](#0-0)  configured concretely as `EverythingBut<Equals<AssetHubLocation>>` in the Bridge Hub Westend runtime [2](#0-1) . This is an exact-equality denylist for a single location, not an allowlist scoped to the caller's own authenticated identity.

### Finding Description
`convert()` reads `AliasOrigin(origin)` straight out of the XCM program body (data the calling AssetHub account fully controls via `pallet_xcm::execute` / `transfer_assets_using_type_and_then`'s custom `remote_xcm`), checks only that it is not literally equal to `AssetHubLocation`, then converts it into an agent id used for message origin and Ethereum-side authorization [3](#0-2) . This mirrors the ethstore bug's flaw exactly: a field that a client-supplied structure claims about "who this belongs to" is accepted without re-deriving/verifying it matches the entity that actually produced the message (there, the on-disk KeyFile's crypto-derived address; here, the sending parachain's true universal-origin identity, which is instead supplied at the level of BH's own `universal_source` check, not the *remote* program's embedded `AliasOrigin`).

The prior fix (PR #12159, `prdoc/pr_12159.prdoc`) explicitly acknowledges the exploit class — "blocks an origin-spoofing attack vector ... protecting the bridge's primary agent account (derived from the Asset Hub Root location)" [4](#0-3)  — but implements it as a single hard-coded exclusion (`Equals<AssetHubLocation>`) rather than restricting `AliasOrigin` targets to the location the *actual* message sender is entitled to alias into (the same `Aliasers`/`TrustedAliasers` model already used by the local XCM barrier for in-chain execution [5](#0-4) ). Any other parachain location — e.g. another system parachain, the Relay Chain root, or any external chain that also uses Snowbridge and therefore holds an Ethereum-side agent contract with real ERC-20 balances/allowances — is *not* excluded, since it is not `Equals<AssetHubLocation>`.

### Impact Explanation
An unprivileged AssetHub account can submit an XCM via `pallet_xcm::execute`/`transfer_assets_using_type_and_then` embedding `AliasOrigin(<victim-parachain-location>)` for any parachain other than AssetHub that has a pre-existing agent on Ethereum. The converter accepts it, produces a `Message` whose `origin` is the victim's agent id, and the Gateway contract will execute the attached `Transact`/`Command::CallContract` under that victim agent's authority — enabling theft or misuse of funds/allowances the victim agent holds on Ethereum. This is unauthorized execution/origin escalation with theft or unbacked movement of bridge-held value, matching the "unauthorized execution or origin escalation" and "theft ... from bridge-state" impact categories.

### Likelihood Explanation
The path requires only a signed AssetHub account (no relayer, validator, governance, or leaked-key assumption) and standard, already-available extrinsics (`pallet_xcm::execute` / `transfer_assets_using_type_and_then` with a custom `remote_xcm`). The denylist is a single hard equality check that is trivially bypassed by choosing any other real location string, so no cryptographic or consensus barrier stops it once the message reaches `XcmConverter::convert()`.

### Recommendation
Replace the single-location denylist with an allowlist tied to the message's authenticated sending identity (mirroring `TrustedAliasers`/`AuthorizedAliasers` semantics already used for local XCM execution): only allow `AliasOrigin` to target locations the actual universal-source (`local_sub`/`para_id`) is authorized to alias into (e.g. itself or an explicitly authorized child/self location), instead of `EverythingBut<Equals<AssetHubLocation>>`.

### Proof of Concept
1. Ensure a victim parachain `V` (≠ AssetHub) has a registered Ethereum agent holding assets/allowances (already the case for system/appchains bridging via Snowbridge).
2. As an unprivileged AssetHub signer, call `pallet_xcm::execute` (or `transfer_assets_using_type_and_then`) with a `remote_xcm` containing:
   `WithdrawAsset(fee) -> PayFees -> ReserveAssetDeposited/WithdrawAsset(assets) -> AliasOrigin(Location::new(1,[Parachain(V)])) -> DepositAsset{beneficiary: attacker} -> Transact{arbitrary contract call} -> SetTopic`.
3. This passes `AllowedAliasOrigin::contains` since `Location::new(1,[Parachain(V)]) != AssetHubLocation`, matching the negative test at [6](#0-5)  which only asserts rejection for the AssetHub location specifically, not for other parachain locations.
4. The resulting outbound message is delivered to the Gateway with `origin` = agent id of `V`, letting the attacker's `Transact` execute as `V`'s agent on Ethereum.

### Citations

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs (L217-256)
```rust
	pub fn convert(&mut self) -> Result<Message, XcmConverterError> {
		// Get fee amount
		let fee_amount = self.extract_remote_fee()?;

		// Get ENA reserve asset from WithdrawAsset.
		let mut enas =
			match_expression!(self.peek(), Ok(WithdrawAsset(reserve_assets)), reserve_assets);
		if enas.is_some() {
			let _ = self.next();
		}

		// Get PNA reserve asset from ReserveAssetDeposited
		let pnas = match_expression!(
			self.peek(),
			Ok(ReserveAssetDeposited(reserve_assets)),
			reserve_assets
		);
		if pnas.is_some() {
			let _ = self.next();
		}

		// Try to get ENA again if it is after PNA
		if enas.is_none() {
			enas =
				match_expression!(self.peek(), Ok(WithdrawAsset(reserve_assets)), reserve_assets);
			if enas.is_some() {
				let _ = self.next();
			}
		}
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

**File:** cumulus/parachains/runtimes/bridge-hubs/bridge-hub-westend/src/bridge_to_ethereum_config.rs (L74-81)
```rust
pub type SnowbridgeExporterV2 = EthereumBlobExporterV2<
	UniversalLocation,
	EthereumNetwork,
	EthereumOutboundQueueV2,
	EthereumSystemV2,
	AssetHubParaId,
	EverythingBut<Equals<AssetHubLocation>>,
>;
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

**File:** cumulus/parachains/runtimes/bridge-hubs/bridge-hub-rococo/src/xcm_config.rs (L180-184)
```rust
/// Defines origin aliasing rules for this chain.
///
/// - Allow any origin to alias into a child sub-location (equivalent to DescendOrigin),
/// - Allow origins explicitly authorized by the alias target location.
pub type TrustedAliasers = (AliasChildLocation, AuthorizedAliasers<Runtime>);
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
