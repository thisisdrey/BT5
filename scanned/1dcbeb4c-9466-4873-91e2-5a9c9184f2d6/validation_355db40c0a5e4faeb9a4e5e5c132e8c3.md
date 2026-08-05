Excellent finding — this is a directly analogous, already-documented instance of the exact bug class described in the report: a location/identifier that is correctly constructed for one side of a cross-chain (Ethereum→BridgeHub→AssetHub) flow being mismatched when a *different* code path (the claim/lookup side) reconstructs the equivalent key, causing the intended state transition (fund recovery) to fail. It was found and fixed in this very repository, confirming the pattern is realistic and previously exploitable here.

### Title
Asset-trap/claim key mismatch from inconsistent `network` field in fallback claimer location permanently locks bridged funds - (File: `bridges/snowbridge/primitives/inbound-queue/src/v2/converter.rs`, fixed alongside `polkadot/xcm/pallet-xcm/src/lib.rs`)

### Summary
Snowbridge's inbound-queue-v2 message converter builds a fallback "claimer" `Location` (the Snowbridge sovereign/bridge-owner account on AssetHub) whenever an inbound Ethereum message doesn't supply an explicit claimer. When execution of the derived remote XCM fails or leaves leftover assets, `pallet_xcm::DropAssets::drop_assets` on AssetHub hashes `(origin, assets)` — where `origin` is exactly this claimer `Location` — into `AssetTraps`. To later recover the funds, the bridge owner calls `pallet_xcm::claim_assets`, whose `ClaimAssets::claim_assets` implementation independently derives the caller's `Location` via `T::ExecuteXcmOrigin`/`SignedToAccountId32` (which always sets `network: Some(LocalNetwork)`), then recomputes the same hash to look up `AssetTraps`. Before the fix identified by PR `pr_11919.prdoc`, the converter's fallback claimer used `network: None`, producing a `Location` that never equals the one produced by `SignedToAccountId32` for the same signed account — so the trap-key computed at drop time could never be reproduced at claim time.

### Finding Description
This is a structural analog to the Sherlock H-4 report's root cause: a value ("the destination-chain lToken") is correctly derived and used to key state in one code path (the initial write), but a second, independent code path that must reconstruct an equivalent key for state lookup/consumption computes it differently, so the "find" always fails.

Here:
- Write path: `MessageToXcm::prepare` (v2 converter) synthesizes `claimer = AccountId32 { network: None, id: bridge_owner }` as fallback (pre-fix) — this becomes `origin` in `DropAssets::drop_assets` at `polkadot/xcm/pallet-xcm/src/lib.rs` (`AssetTraps::<T>::mutate(hash, ...)` where `hash = BlakeTwo256::hash_of(&(&origin, &versioned))`).
- Read path: `claim_assets` extrinsic (`polkadot/xcm/pallet-xcm/src/lib.rs` `claim_assets`) uses `T::ExecuteXcmOrigin::ensure_origin(origin)` to convert the caller's signed account into a `Location`. On AssetHub this uses `SignedToAccountId32<_, _, LocalNetwork>`, which always sets `network: Some(LocalNetwork::get())`.
- Because `AssetId`/`Location` equality is exact (including the `network` junction field), `hash_of(&(origin_write, assets))` != `hash_of(&(origin_read, assets))`, so `AssetTraps::<T>::get(hash)` returns `0` and `ClaimAssets::claim_assets` returns `None`, causing `XcmError::UnknownClaim`.

Existing guards do not stop this: `claim_assets` performs no fallback/normalization between `network: None` and `network: Some(_)` variants of an otherwise-identical account, and `AssetTraps` is a flat hash map with no secondary index to recover from a mismatched key. This mirrors the audited bug exactly: a chain/context-specific representation (Chain A's lToken vs. Chain B's; here, "unset network" vs. "chain's own network") is embedded into a value that is later used as a lookup key by logic that assumes the *other* representation, and the mismatch is silent — no explicit error surfaces the incompatibility, it just returns "not found."

### Impact Explanation
Funds routed through Snowbridge's inbound-queue-v2 without an explicit claimer (e.g., malformed/invalid remote XCM payloads, or any scenario producing leftover holding) are trapped against a claimer key that the bridge owner's own `claim_assets` call could never reconstruct. This is a permanent user/bridge-fund lock — the assets remain forever inaccessible via the intended recovery path, matching the "permanent user-fund or bridge-state lock" impact category. Given Snowbridge routes real bridged ETH/ERC-20 value, this is a direct value-loss bug, not merely a UX inconvenience.

### Likelihood Explanation
Reachable without any privileged actor: any relayer can submit a syntactically valid but semantically malformed/invalid `remote_xcm` payload (or otherwise cause partial execution leaving assets in holding) for a message with no `claimer` field set, which is fully attacker/relayer-controllable input on the public inbound queue. No malicious validator, governance action, or leaked key is required — this is a pure logic defect in cross-context key derivation reachable through the normal, permissionless bridge message flow.

### Recommendation
Ensure `Location` values used as trap/claim keys are canonicalized identically regardless of which code path constructs them: derive the fallback claimer using the same `LocalNetwork`-tagged converter that `claim_assets`'s origin conversion uses (as done in the actual fix, setting `network: Some(LocalNetwork::get())`), and more generally, add a defensive equivalence/canonicalization layer (or reject `network: None` vs `network: Some(_)` ambiguity) anywhere a `Location` is hashed for storage keys that must later be reconstructed by independent origin-conversion logic.

### Proof of Concept
This exact scenario, including the vulnerable code path and its fix, is captured by the repository's own regression test and PR doc: [1](#0-0) [2](#0-1) 

Pre-fix reconstruction:
1. Relayer submits an `EthereumInboundQueueV2::process_message` with `claimer: None` and an invalid/garbage `Payload::Raw` XCM (e.g. as in the test at lines 1057–1072), causing non-fee assets to remain in holding on AssetHub.
2. AssetHub's XCM executor calls `pallet_xcm::DropAssets::drop_assets` with `origin = AccountId32 { network: None, id: bridge_owner }` (pre-fix), storing `AssetTraps[hash(origin, assets)] += 1`: [3](#0-2) 
3. Bridge owner calls `PolkadotXcm::claim_assets(signed(bridge_owner), assets, beneficiary)`. Internally `T::ExecuteXcmOrigin::ensure_origin` converts the signed origin via `SignedToAccountId32<_, _, LocalNetwork>`, always producing `AccountId32 { network: Some(LocalNetwork), id: bridge_owner }` — a *different* `Location* from the one used to compute the stored hash: [4](#0-3) 
4. `AssetTraps::<T>::get(hash)` returns `0`, `claim_assets` returns `None`, and the outer `ClaimAsset` XCM instruction fails with `XcmError::UnknownClaim`, permanently stranding the trapped bridged assets. [5](#0-4)

### Citations

**File:** prdoc/stable2603-3/pr_11919.prdoc (L1-19)
```text
title: 'Snowbridge: Set default asset claimer to local network'
doc:
- audience: Runtime Dev
  description: |-
    The inbound-queue v2 message converter falls back to the Snowbridge sovereign
    account on AssetHub as the asset claimer when no explicit claimer is supplied.
    Previously this fallback used `AccountId32 { network: None, .. }`, which did
    not match the location AssetHub's signed-origin converter produces (it sets
    `network: Some(LocalNetwork)`). The trap-key hash stored on `AssetsTrapped`
    therefore could not be matched by a signed `polkadotXcm.claim_assets` call,
    making default-claimer trapped funds effectively unrecoverable without a
    runtime upgrade.

    This PR sets `network: Some(LocalNetwork::get())` on the fallback claimer so
    its `Location` agrees with what `SignedToAccountId32<_, _, LocalNetwork>`
    yields on AssetHub, and adds a test covering the no-claimer-supplied path.
crates:
- name: snowbridge-inbound-queue-primitives
  bump: patch
```

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_inbound.rs (L1023-1032)
```rust
/// When an inbound message arrives without a (valid) claimer, the converter falls back
/// to the bridge owner sovereign account, anchored on the local network. This test
/// drives the full claim flow: invalid XCM payload causes the holding register to be
/// trapped against that fallback location on Asset Hub, and the bridge owner signed
/// origin then claims the trapped assets via `pallet_xcm::claim_assets`.
///
/// Before the fix, the fallback claimer used `network: None`, so the trap origin did
/// not match the location produced by Asset Hub's `SignedToAccountId32` converter
/// (which always tags the local network), and the claim would fail with `UnknownClaim`.
#[test]
```

**File:** polkadot/xcm/pallet-xcm/src/lib.rs (L3901-3924)
```rust
impl<T: Config> DropAssets for Pallet<T> {
	fn drop_assets(origin: &Location, holding: AssetsInHolding, _context: &XcmContext) -> Weight {
		if holding.is_empty() {
			return Weight::zero();
		}
		let assets: Vec<Asset> = holding.assets_iter().collect();
		// SAFETY: "forget" about any fungible imbalances so that they are not dropped/resolved
		// here. The mirrored asset claiming operation will "recover" the imbalances by minting
		// back into holding, effectively duplicating the imbalance and only then dropping the
		// duplicate. As a result, total issuance doesn't change.
		holding.fungible.into_iter().for_each(|(_, mut accounting)| {
			accounting.forget_imbalance();
		});
		let versioned = VersionedAssets::from(Assets::from(assets));
		let hash = BlakeTwo256::hash_of(&(&origin, &versioned));
		AssetTraps::<T>::mutate(hash, |n| *n += 1);
		Self::deposit_event(Event::AssetsTrapped {
			hash,
			origin: origin.clone(),
			assets: versioned,
		});
		// TODO #3735: Put the real weight in there.
		Weight::zero()
	}
```

**File:** polkadot/xcm/pallet-xcm/src/lib.rs (L3927-3950)
```rust
impl<T: Config> ClaimAssets for Pallet<T> {
	fn claim_assets(
		origin: &Location,
		ticket: &Location,
		assets: &Assets,
		context: &XcmContext,
	) -> Option<AssetsInHolding> {
		let mut versioned = VersionedAssets::from(assets.clone());
		match ticket.unpack() {
			(0, [GeneralIndex(i)]) => {
				versioned = match versioned.into_version(*i as u32) {
					Ok(v) => v,
					Err(()) => return None,
				}
			},
			(0, []) => (),
			_ => return None,
		};
		let hash = BlakeTwo256::hash_of(&(origin.clone(), versioned.clone()));
		match AssetTraps::<T>::get(hash) {
			0 => return None,
			1 => AssetTraps::<T>::remove(hash),
			n => AssetTraps::<T>::insert(hash, n - 1),
		}
```

**File:** bridges/snowbridge/primitives/inbound-queue/src/v2/converter.rs (L123-143)
```rust
	/// Parse the message into an intermediate form, with all fields decoded
	/// and prepared.
	fn prepare(message: Message) -> Result<PreparedMessage, ConvertMessageError> {
		// ETH "asset id" is the Ethereum root location. Same location used for the "bridge owner".
		let ether_location = Location::new(2, [GlobalConsensus(EthereumNetwork::get())]);
		let bridge_owner = Self::bridge_owner()?;

		let claimer = message
			.claimer
			// Get the claimer from the message,
			.and_then(|claimer_bytes| Location::decode(&mut claimer_bytes.as_ref()).ok())
			// or use the Snowbridge sovereign on AH as the fallback claimer.
			.unwrap_or_else(|| {
				Location::new(
					0,
					[AccountId32 {
						network: Some(LocalNetwork::get()),
						id: bridge_owner.clone().into(),
					}],
				)
			});
```
