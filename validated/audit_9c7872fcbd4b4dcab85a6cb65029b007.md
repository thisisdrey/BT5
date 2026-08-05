This confirms the runtime configuration at line 80 exactly: `EverythingBut<Equals<AssetHubLocation>>` is passed as `AllowedAliasOrigin` in `bridge_to_ethereum_config.rs`, matching the claim's citation of `prdoc/pr_12159.prdoc`.

All claim elements are verified against the actual repository code:
1. `EthereumBlobExporter::validate` (v2) derives `para_id` from the authenticated `universal_source` and restricts it to AssetHub only, but never passes this authenticated identity into `XcmConverter::new` — confirmed at [1](#0-0) , versus v1's `EthereumBlobExporter::validate`, which computes `agent_id` from `source_location` and explicitly passes it into the v1 `XcmConverter::new` at [2](#0-1) .
2. `XcmConverter::convert` in v2 pulls `origin_location` directly from the payload's `AliasOrigin` instruction and only checks it against `AllowedAliasOrigin::contains`, never against the authenticated sender [3](#0-2) .
3. This `origin` is placed directly into the final `Message` struct sent to the Gateway [4](#0-3) .
4. The runtime's actual `AllowedAliasOrigin` type parameter is `EverythingBut<Equals<AssetHubLocation>>`, confirmed live in `bridge-hub-westend`'s `bridge_to_ethereum_config.rs` [5](#0-4) , which blocks only the exact AssetHub location and allows every other `Location` (sibling parachains, BridgeHub's own agent, etc.) to be claimed as the message origin.

The code precisely matches the claim's description: the only defense is a single denylist entry protecting AssetHub's own sovereign agent (added specifically to close the forgery vector described in `pr_12159.prdoc`), while every other registered agent identity remains forgeable by any unprivileged AssetHub XCM caller through the `AliasOrigin` instruction embedded in a `remote_xcm` payload of an `InitiateTransfer`. This is a structural, reachable, unauthorized-origin-escalation bug matching the "unauthorized execution or origin escalation" and "theft or unbacked mint or unlock" categories of the impact gate, since the resulting `Message.origin` selects which Ethereum-side agent contract (and its held Ether) executes the `CallContract` command.

Audit Report

## Title
V2 Snowbridge outbound converter binds `Message.origin` to an attacker-supplied `AliasOrigin` location instead of the authenticated sender, letting any AssetHub XCM caller impersonate another chain's agent - (File: `bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs`)

## Summary
The V2 Snowbridge outbound converter derives the `Message.origin` field — which selects the Ethereum-side agent contract that executes bridge commands, including `CallContract` with attacker-controlled `value`/`calldata` — purely from an `AliasOrigin` instruction embedded in the attacker-supplied XCM payload, checked only against a denylist (`EverythingBut<Equals<AssetHubLocation>>`) rather than bound to the authenticated sender. Any signed AssetHub account can set `AliasOrigin` to any location other than AssetHub's own (e.g. a sibling parachain's sovereign location or BridgeHub's governance location) to have the Gateway execute commands "as" that victim agent.

## Finding Description
`EthereumBlobExporter::validate` (v2) authenticates the caller via `universal_source`, extracting `para_id` and enforcing it equals `AssetHubParaId` [6](#0-5) . However, this authenticated identity is discarded — it is never passed into `XcmConverter::new`, unlike the v1 exporter which computes an `agent_id` from `source_location` and explicitly threads it through [2](#0-1) . Instead, `XcmConverter::convert` extracts `origin_location` straight from the `AliasOrigin` instruction in the XCM payload — fully attacker-controlled data — and validates it only against the configured `AllowedAliasOrigin::contains` filter [3](#0-2) . The derived `origin` (an agent id) is placed directly into the resulting `Message` [4](#0-3) . In the live `bridge-hub-westend` runtime, `AllowedAliasOrigin` is configured as `EverythingBut<Equals<AssetHubLocation>>` [5](#0-4) , i.e. it blocks exactly one location (AssetHub's own sovereign location) and permits every other `Location` to be claimed as origin. There is no check that `origin_location` corresponds to (or is a sub-location of) the actual authenticated sender identified during `validate`.

## Impact Explanation
`Message.origin` selects the agent contract the Gateway executes commands as, including `CallContract { target, calldata, gas, value }` where value can draw on Ether held by that agent contract. Because the denylist only excludes AssetHub's own location, an unprivileged AssetHub account can forge `AliasOrigin` to any other registered agent (sibling parachain sovereign locations, BridgeHub's own `Location::here()` governance agent, etc.) and have the Gateway execute arbitrary contract calls as that victim agent — an unauthorized origin escalation that can result in theft of bridge-held Ether/assets.

## Likelihood Explanation
The exploit requires only a signed AssetHub account calling `pallet_xcm::execute` with a crafted `InitiateTransfer`/`remote_xcm` — no governance, relayer, or validator compromise needed. Target agent locations are largely predictable (e.g., `Location::here()`, `Location::new(1,[Parachain(id)])`), and the repository's own regression test scaffolding (`signed_assethub_user_cannot_bypass_origin_alteration_when_routing_to_ethereum`) demonstrates the exact mechanics needed, only substituting the denylisted AssetHub location for any other valid location.

## Recommendation
Bind `Message.origin` to the authenticated sender's `agent_id` (computed from `universal_source`/`para_id` as in v1) rather than trusting an unauthenticated `AliasOrigin` payload value checked only against a denylist. If `AliasOrigin` must remain supported for refinement, validate that it is bound to (a sub-location of, or otherwise provably owned by) the authenticated sender, and replace `EverythingBut<Equals<AssetHubLocation>>` with an allowlist keyed to the real, authenticated origin.

## Proof of Concept
1. As any signed AssetHub account, call `PolkadotXcm::execute` with an XCM containing `InitiateTransfer{ remote_xcm: [..., AliasOrigin(victim_location), DepositAsset{...}, Transact{ call: CallContract{ target: attacker_addr, value: agent_balance, ...} }, SetTopic(..)] }`, mirroring `signed_assethub_user_cannot_bypass_origin_alteration_when_routing_to_ethereum` but substituting a sibling-parachain or BridgeHub location for `forged_assethub_origin`.
2. `AllowedAliasOrigin::contains(victim_location)` returns true because only `AssetHubLocation` is denylisted, so `ensure!` in `convert.rs:254` passes.
3. `AgentIdOf::convert_location(victim_location)` yields the victim's real agent id, which becomes `Message.origin`.
4. On Ethereum, the Gateway executes `CallContract` under the victim's agent authority/funds without the victim ever authorizing the action.

### Citations

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

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/converter/mod.rs (L149-150)
```rust
		let mut converter =
			XcmConverter::<ConvertAssetId, (), AllowedAliasOrigin>::new(&message, expected_network);
```

**File:** bridges/snowbridge/primitives/outbound-queue/src/v1/converter/mod.rs (L102-118)
```rust
		let source_location = Location::new(1, local_sub.clone());

		let agent_id = match AgentHashedDescription::convert_location(&source_location) {
			Some(id) => id,
			None => {
				tracing::error!(target: "xcm::ethereum_blob_exporter", ?source_location, "unroutable due to not being able to create agent id.");
				return Err(SendError::NotApplicable);
			},
		};

		let message = message.take().ok_or_else(|| {
			tracing::error!(target: "xcm::ethereum_blob_exporter", "xcm message not provided.");
			SendError::MissingArgument
		})?;

		let mut converter =
			XcmConverter::<ConvertAssetId, ()>::new(&message, expected_network, agent_id);
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
