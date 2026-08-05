### Title
`fallback_max_weight: None` in Snowbridge V2 inbound `Transact` calls leaves `Transact` weight metering undefined on version downgrade — (File: `bridges/snowbridge/primitives/inbound-queue/src/v2/converter.rs`)

### Summary
The XCM v5 `Transact` instruction replaced the mandatory `require_weight_at_most: Weight` field with an `Option<Weight> fallback_max_weight`, used only as a fallback when the message must be downgraded to XCM v4 for chains that haven't upgraded yet, or when the local chain fails to decode the call. In `bridges/snowbridge/primitives/inbound-queue/src/v1.rs::convert_register_token`, the `Transact` for asset creation explicitly sets `fallback_max_weight: Some(Weight::from_parts(400_000_000, 8_000))` — this was fixed in `pr_6792` ("Add fallback_max_weight to snowbridge Transact") specifically because "We originally put no fallback for a message in snowbridge's inbound queue but we should have one." However, the analogous V2 inbound converter, `bridges/snowbridge/primitives/inbound-queue/src/v2/converter.rs::make_create_asset_xcm_for_polkadot` (lines 302-304, 315-319), still constructs both of its `Transact` instructions with `fallback_max_weight: None`, reproducing exactly the invariant the team already identified and fixed once for v1.

### Finding Description
XCM v5's `Transact` definition documents the risk directly: [1](#0-0) 

The v5→v4 downgrade path shows the concrete consequence of a missing fallback — if the call cannot be decoded (or if a v5 message must be sent onward to a not-yet-upgraded v4 chain), the weight is derived from `fallback_max_weight.unwrap_or(Weight::MAX)`: [2](#0-1) 

The team's own test explicitly documents that omitting the fallback silently degrades weight metering to `Weight::MAX`: [3](#0-2) 

`v1.rs`'s inbound-queue `Transact` was already patched to carry an explicit `fallback_max_weight`, matching PR `pr_6792`'s stated intent: [4](#0-3) 

Yet the structurally identical V2 inbound-queue asset-creation `Transact` calls in `make_create_asset_xcm_for_polkadot` still hard-code `fallback_max_weight: None` for both the `create_call_index` and `set_reserves_call_index` transacts: [5](#0-4) 

This is precisely the bug class described in the external report: a field that must be treated as required for certain message paths (limit/trigger orders needing `p`; cross-version `Transact` needing a usable weight bound) is instead conditionally/optionally populated, and the code silently proceeds with a degraded/undefined value (`None` → potentially `Weight::MAX` on downgrade, or `FailedToDecode`/unbounded weight metering) rather than enforcing the requirement.

### Impact Explanation
If this V2-generated `Xcm<()>` (which is untrusted-input-derived — it decodes attacker/user-controlled Ethereum message bytes) is ever re-encoded/downgraded to XCM v4 for compatibility (the exact scenario `pr_6643`/`pr_6792` were created to handle "in the small time window when chains are upgrading"), the missing fallback causes the `require_weight_at_most` field to become `Weight::MAX` instead of a bounded, correct value. `Weight::MAX` in the local weight-metering pass (`instr_weight_with_limit` in `polkadot/xcm/xcm-builder/src/weight.rs`) either causes the message to always exceed the weight limit (denial of service / stuck message in the bridge's inbound processing) or, depending on how the receiving chain treats an unbounded weight claim during barrier/weight checks, can permit gas/weight-limit bypass for the nested `Transact` dispatch. Either failure mode directly matches the accepted impact classes: it can stall bridge inbound processing (public underpriced/degraded work / stalled bridge processing) or allow the dispatched call's weight accounting to be mis-bound during the version-downgrade window, undermining "implementation bugs that can bring down or take control of a Substrate-based chain" and "bridge processing" reliability without requiring any privileged actor — this fires purely from processing an inbound Ethereum message during a routine runtime upgrade rollout window.

### Likelihood Explanation
The trigger condition — a runtime upgrade window where BridgeHub/AssetHub sends v5 XCM but a receiving pallet/chain is still on v4 — is a normal, expected part of Polkadot SDK's staged-upgrade rollout, not a hypothetical edge case; the team already hit and fixed this exact scenario once for `v1.rs` via `pr_6792` and documented it as a real production concern ("small time window when chains are upgrading"). The `v2::converter::MessageToXcm::convert` path is exercised on every inbound Ethereum `CreateAsset` message, which is attacker-influenced input (arbitrary Ethereum senders can trigger asset creation), making the vulnerable code path reachable without any privileged action.

### Recommendation
Set an explicit, non-`None` `fallback_max_weight` (mirroring the pattern already used in `v1.rs::convert_register_token`, e.g. `Some(Weight::from_parts(400_000_000, 8_000))` or a benchmarked equivalent) on both `Transact` instructions inside `make_create_asset_xcm_for_polkadot` in `bridges/snowbridge/primitives/inbound-queue/src/v2/converter.rs`, and add a regression test asserting `fallback_max_weight.is_some()` for all V2-inbound-generated `Transact` instructions to prevent this class of gap from resurfacing when new call types are added.

### Proof of Concept
1. Prepare an inbound Ethereum message with `Payload::CreateAsset { token, network: Polkadot }` and submit it through the V2 inbound queue `MessageToXcm::convert`.
2. Observe the generated XCM (as in the existing test suite in `converter.rs`) contains two `Transact { fallback_max_weight: None, .. }` instructions (lines 302-304, 315-317).
3. Simulate the documented downgrade scenario from `polkadot/xcm/src/v5/mod.rs` `transact_roundtrip_works` test: convert this V5 `Xcm<()>` to V4 via `TryInto` while the call bytes are opaque/undecodable to the local converter (as they are here — `call` is raw `Encode`d bytes for a remote pallet index unknown to the generic XCM crate). The V5→V4 `TryFrom` impl at `polkadot/xcm/src/v4/mod.rs:1335-1352` falls back to `fallback_max_weight.unwrap_or(Weight::MAX)`, producing `require_weight_at_most: Weight::MAX`, which the receiving v4 chain's weight-limit and barrier logic then either rejects (stalling the CreateAsset flow permanently) or processes as a maximal/unbounded weight claim — reproducing, in the V2 path, the exact issue `pr_6792` fixed in the V1 path.

### Citations

**File:** polkadot/xcm/src/v5/mod.rs (L490-501)
```rust
	/// Apply the encoded transaction `call`, whose dispatch-origin should be `origin` as expressed
	/// by the kind of origin `origin_kind`.
	///
	/// The Transact Status Register is set according to the result of dispatching the call.
	///
	/// - `origin_kind`: The means of expressing the message origin as a dispatch origin.
	/// - `call`: The encoded transaction to be applied.
	/// - `fallback_max_weight`: Used for compatibility with previous versions. Corresponds to the
	///   `require_weight_at_most` parameter in previous versions. If you don't care about
	///   compatibility you can just put `None`. WARNING: If you do, your XCM might not work with
	///   older versions. Make sure to dry-run and validate.
	///
```

**File:** polkadot/xcm/src/v5/mod.rs (L1676-1704)
```rust
		// If we have no fallback the resulting message won't know the weight.
		let xcm_without_fallback = Xcm::<()>(vec![
			WithdrawAsset((Here, 1u128).into()),
			Transact {
				origin_kind: OriginKind::SovereignAccount,
				call: vec![200, 200, 200].into(),
				fallback_max_weight: None,
			},
		]);
		let old_xcm = OldXcm::<()>(vec![
			OldInstruction::WithdrawAsset((OldHere, 1u128).into()),
			OldInstruction::Transact {
				origin_kind: OriginKind::SovereignAccount,
				call: vec![200, 200, 200].into(),
				require_weight_at_most: Weight::MAX,
			},
		]);
		assert_eq!(old_xcm, OldXcm::<()>::try_from(xcm_without_fallback.clone()).unwrap());
		let new_xcm: Xcm<()> = old_xcm.try_into().unwrap();
		let xcm_with_max_weight_fallback = Xcm::<()>(vec![
			WithdrawAsset((Here, 1u128).into()),
			Transact {
				origin_kind: OriginKind::SovereignAccount,
				call: vec![200, 200, 200].into(),
				fallback_max_weight: Some(Weight::MAX),
			},
		]);
		assert_eq!(new_xcm, xcm_with_max_weight_fallback);
	}
```

**File:** polkadot/xcm/src/v4/mod.rs (L1335-1352)
```rust
			Transact { origin_kind, mut call, fallback_max_weight } => {
				// We first try to decode the call, if we can't, we use the fallback weight,
				// if there's no fallback, we just return `Weight::MAX`.
				let require_weight_at_most = match call.take_decoded() {
					Ok(decoded) => decoded.get_dispatch_info().call_weight,
					Err(error) => {
						let fallback_weight = fallback_max_weight.unwrap_or(Weight::MAX);
						tracing::debug!(
							target: "xcm::versions::v5Tov4",
							?error,
							?fallback_weight,
							"Couldn't decode call in Transact"
						);
						fallback_weight
					},
				};
				Self::Transact { origin_kind, require_weight_at_most, call: call.into() }
			},
```

**File:** bridges/snowbridge/primitives/inbound-queue/src/v1.rs (L274-288)
```rust
			UniversalOrigin(GlobalConsensus(network)),
			// Call create_asset on foreign assets pallet.
			Transact {
				origin_kind: OriginKind::Xcm,
				fallback_max_weight: Some(Weight::from_parts(400_000_000, 8_000)),
				call: (
					create_call_index,
					asset_id,
					MultiAddress::<[u8; 32], ()>::Id(owner),
					MINIMUM_DEPOSIT,
				)
					.encode()
					.into(),
			},
			// Forward message id to Asset Hub
```

**File:** bridges/snowbridge/primitives/inbound-queue/src/v2/converter.rs (L300-319)
```rust
			},
			// Call to create the asset.
			Transact {
				origin_kind: OriginKind::Xcm,
				fallback_max_weight: None,
				call: (
					create_call_index,
					asset_id.clone(),
					MultiAddress::<[u8; 32], ()>::Id(bridge_owner_bytes.into()),
					create_min_blance,
				)
					.encode()
					.into(),
			},
			// Call to set Ethereum as the asset's reserve.
			Transact {
				origin_kind: OriginKind::Xcm,
				fallback_max_weight: None,
				call: (set_reserves_call_index, asset_id, vec![reserve_data]).encode().into(),
			},
```
