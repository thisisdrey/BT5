Confirmed: the code exactly matches the claim. `XcmConverter::convert()` reads `AliasOrigin(origin)` from the untrusted `remote_xcm`, checks only `AllowedAliasOrigin::contains(origin_location)`, and directly derives `Message.origin` via `AgentIdOf::convert_location(origin_location)` — with no cross-check against the actual XCM-executor-verified sending origin. [1](#0-0) 

In the Bridge Hub Westend runtime, `SnowbridgeExporterV2` is instantiated with `AllowedAliasOrigin = EverythingBut<Equals<AssetHubLocation>>`, i.e., the filter blocks only the exact AssetHub location and allows every other `Location` (including any other sibling parachain's location) to pass the check. [2](#0-1) 

Audit Report

## Title
Snowbridge V2 outbound `AliasOrigin` filter only blocks AssetHub, letting any other chain forge an arbitrary sibling's Agent identity - (File: `bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs`)

## Summary
`XcmConverter::convert()` derives the outbound `Message.origin` (an `AgentId`) directly from an attacker-supplied `AliasOrigin` instruction embedded in `remote_xcm`, gated only by an `AllowedAliasOrigin: Contains<Location>` filter. On Bridge Hub Westend this filter is `EverythingBut<Equals<AssetHubLocation>>`, which blocks only the AssetHub location and permits any other `Location` — including any other sibling parachain's location — to pass unchecked.

## Finding Description
`XcmConverter::convert()` parses the `remote_xcm` syntactically rather than under XCM-executor origin semantics: it takes the location supplied inside the message's own `AliasOrigin(origin)` instruction, checks it only against `AllowedAliasOrigin::contains(origin_location)`, and converts it directly into the `AgentId` placed into `Message.origin`. [1](#0-0)  This value is then embedded unchanged into the outgoing `Message` sent to the Ethereum Gateway. [3](#0-2) 

The Bridge Hub Westend runtime wires `AllowedAliasOrigin` to `EverythingBut<Equals<AssetHubLocation>>` for `SnowbridgeExporterV2`. [2](#0-1)  This is a denylist that excludes exactly one specific location (AssetHub) rather than an allowlist enforcing "aliased origin must equal the true/verified sender." Consequently, any caller able to submit an `InitiateTransfer{preserve_origin: true, remote_xcm: [...AliasOrigin(X), ...]}` message where `X` is any `Location` other than AssetHub (e.g., another sibling parachain's location) will pass the `AllowedAliasOrigin::contains` check, and `AgentIdOf::convert_location(X)` will derive that other chain's `AgentId` for use as `Message.origin` — an identity the actual caller does not own.

## Impact Explanation
This matches the "unauthorized execution or origin escalation" category in the impact gate: Snowbridge Agents are per-chain sovereign accounts on Ethereum used as execution context for Gateway commands such as `CallContract` and `UnlockNativeToken`. A forged `Message.origin` (the `AgentId`) lets an attacker cause commands to execute as if authorized by a victim sibling chain's Agent, potentially triggering unauthorized contract calls or asset unlocks attributed to that Agent's identity/holdings on Ethereum. The exact corrupted value is `Message.origin` (the `AgentId` derived from an unverified `AliasOrigin` location).

## Likelihood Explanation
The path is reachable by any unprivileged signed account on any parachain capable of executing `pallet_xcm::execute`/`send` with a self-authored `remote_xcm` containing `WithdrawAsset`/`PayFees`/`InitiateTransfer{preserve_origin:true, remote_xcm:[...]}`. The `remote_xcm` bytes, including the `AliasOrigin` instruction, are attacker-controlled content not subject to per-instruction origin authorization before reaching `XcmConverter`. No relayer, validator, governance, or leaked key is required — only knowledge of the target sibling parachain's `Location`, which is public. The current `AllowedAliasOrigin::contains` check in `convert.rs` and its Westend-runtime instantiation as `EverythingBut<Equals<AssetHubLocation>>` are demonstrably insufficient to block this for any location other than AssetHub. [4](#0-3) [2](#0-1) 

## Recommendation
Do not implement `AllowedAliasOrigin` as a denylist over a single privileged location. Instead, bind the `AliasOrigin` check to the XCM-executor-verified real sending origin of the `InitiateTransfer`/`ExportMessage` call (i.e., require `origin_location == real_origin`), or otherwise derive `Message.origin` from the verified sender rather than from an unauthenticated instruction embedded in attacker-controlled `remote_xcm` bytes.

## Proof of Concept
1. Attacker controls parachain `P` (any sibling other than AssetHub) with a signed account holding sufficient fee assets.
2. Attacker submits via `pallet_xcm::execute` an XCM: `WithdrawAsset`, `PayFees`, `InitiateTransfer{destination: ethereum(), preserve_origin: true, remote_xcm: [AliasOrigin(Location::new(1,[Parachain(VICTIM_PARA_ID)])), DepositAsset{...}, Transact{ContractCall::V1{target, value, calldata, gas}}, SetTopic(...)]}`.
3. In `XcmConverter::convert()`, `AllowedAliasOrigin::contains(victim_para_location)` returns `true` because `EverythingBut<Equals<AssetHubLocation>>` only excludes AssetHub. [4](#0-3) 
4. `AgentIdOf::convert_location(victim_para_location)` derives the victim parachain's `AgentId`, placed into `Message.origin`. [5](#0-4) 
5. When relayed to and executed on the Ethereum Gateway, the `CallContract`/unlock command executes under the victim's Agent context instead of the attacker's own, demonstrating cross-chain origin forgery reachable from an unprivileged, publicly submittable extrinsic.

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

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs (L312-317)
```rust
		let message = Message {
			id: (*topic_id).into(),
			origin,
			fee: fee_amount,
			commands: BoundedVec::try_from(commands).map_err(|_| TooManyCommands)?,
		};
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
