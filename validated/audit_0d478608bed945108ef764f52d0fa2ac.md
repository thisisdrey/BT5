### Title
Permanent, unclaimable fund lock in Snowbridge V1 inbound `SendToken`/`SendNativeToken` XCM when `DepositAsset` to `beneficiary` fails - ([File: bridges/snowbridge/primitives/inbound-queue/src/v1.rs])

### Summary
The external report describes CCTP/Wormhole burning tokens on the source chain while the destination `mintRecipient` becomes permanently unable to receive them (blacklisted), with no mechanism to reroute or reclaim the burned value. The structural analog in this repository is Snowbridge's V1 inbound message converter (`MessageToXcm::convert_send_token` / `convert_send_native_token`), which constructs an XCM program that clears its origin (`ClearOrigin`) and defines no explicit `asset_claimer`/`AliasOrigin` before the final `DepositAsset { beneficiary }`. If that `DepositAsset` fails for any reason (frozen/blocked beneficiary account, unregistered/removed foreign asset, missing ED, etc.), the XCM executor traps the held assets under the *original* execution origin — the BridgeHub sibling-parachain `Location`, not any account-derived location. No signed extrinsic on AssetHub can ever reproduce that origin, so `pallet_xcm::claim_assets` can never match the trap hash. The funds burned/withdrawn on the source side are effectively lost forever, exactly mirroring the CCTP "burnt with no path to mint/recover" failure mode.

### Finding Description
`MessageToXcm::convert_send_token` builds the following (relevant excerpt) for a plain AssetHub destination: [1](#0-0) [2](#0-1) 

Note that `ClearOrigin` runs right after `UniversalOrigin`/`ReserveAssetDeposited`, and the program never sets `asset_claimer` (via `SetHints`) nor uses `AliasOrigin`. Compare this to Snowbridge V2's converter, which added an explicit `claimer` field precisely to fix this same class of bug (documented in `prdoc/stable2603-3/pr_11919.prdoc`): [3](#0-2) 

If `DepositAsset { assets: Wild(AllCounted(2)), beneficiary }` fails (e.g., the beneficiary's `ForeignAssets` account is frozen, the asset was de-registered, or an ED/consumer-reference constraint blocks account creation), `xcm-executor`'s `post_process` traps the still-held assets against a claimer resolved in this priority order: [4](#0-3) 

Because `asset_claimer` is `None` and `context.origin` was cleared to `None` by `ClearOrigin`, the fallback is `self.original_origin` — the origin under which the whole XCM was invoked on AssetHub, i.e., the BridgeHub sibling-parachain `Location` (`Location::new(1, [Parachain(BridgeHubParaId)])`), not an `AccountId32` location. `pallet_xcm::claim_assets` derives its claiming origin from `T::ExecuteXcmOrigin::ensure_origin(origin)`, which for a normal signed extrinsic on AssetHub always yields an `AccountId32`-based location (via `SignedToAccountId32`), never a `Parachain(..)` location: [5](#0-4) [6](#0-5) 

The trap key is `hash(origin, assets)`; since no account-holder can ever present the `Parachain(BridgeHub)` origin from AssetHub, the trapped assets are unconditionally unrecoverable — this is functionally identical to the V2 bug that was fixed in `pr_11919.prdoc` ("fallback claimer... making default-claimer trapped funds effectively unrecoverable without a runtime upgrade"), except V1 never had *any* claimer concept to begin with, so there is no fix path short of a governance/runtime upgrade.

### Impact Explanation
Any failure of the final `DepositAsset` to the user-supplied `beneficiary` in the V1 `SendToken`/`SendNativeToken` inbound flow results in a **permanent, unrecoverable loss of the bridged asset value** — tokens already withdrawn/burned on the Ethereum side, with no realistic path to reclaim them on AssetHub, since the trap origin is architecturally unreachable from any account-based signed extrinsic. This directly matches the "permanent user-fund or bridge-state lock" impact category, and the root cause is a code defect (missing claimer/AliasOrigin design in V1), not a privileged-actor abuse.

### Likelihood Explanation
`DepositAsset` failures are not exotic: a frozen or not-yet-created beneficiary asset account, insufficient existential deposit for the specific `ForeignAssets` instance, or a beneficiary account hitting consumer-reference limits are all realistic conditions that can be engineered or occur naturally for any given `beneficiary`, exactly as in the CCTP blacklist scenario. Given that Snowbridge V1 remains a live production message path (registered token transfers, native token transfers) and the fix applied to V2 (`pr_11919.prdoc`) was never backported/mirrored to V1's converter, this is a currently-exploitable structural gap.

### Recommendation
Add an explicit `asset_claimer`/`AliasOrigin` hint in the V1 `convert_send_token` and `convert_send_native_token` XCM programs, anchored to a location reachable by a normal signed AssetHub extrinsic (e.g., mirroring the V2 fix that pins the fallback claimer to `network: Some(LocalNetwork)`), so that any `DepositAsset` failure traps funds under a claimable location instead of the unreachable BridgeHub sibling-parachain origin.

### Proof of Concept
1. An Ethereum user sends a `SendToken`/`SendNativeToken` message with `Destination::AccountId32 { id }` pointing at an AssetHub account.
2. Before the message is relayed/processed, the target `ForeignAssets` account for that beneficiary becomes unable to receive the deposit (e.g., frozen, de-registered asset, or ED shortfall for that specific asset instance).
3. `EthereumInboundQueue::process_message` converts and executes the XCM on AssetHub; `DepositAsset { .., beneficiary }` fails; `post_process` traps the held asset+fee under `original_origin = Location::new(1, [Parachain(BridgeHub)])` (see `polkadot/xcm/xcm-executor/src/lib.rs:415-420`).
4. No AssetHub signed account can ever produce that `Parachain(BridgeHub)` origin via `pallet_xcm::claim_assets`, so `AssetsTrapped` remains permanently unclaimed — the bridged value is lost, mirroring the CCTP "burnt but unmintable" scenario.

### Citations

**File:** bridges/snowbridge/primitives/inbound-queue/src/v1.rs (L335-342)
```rust
		let mut instructions = vec![
			ReceiveTeleportedAsset(total_fee_asset.into()),
			BuyExecution { fees: asset_hub_fee_asset, weight_limit: Unlimited },
			DescendOrigin(PalletInstance(inbound_queue_pallet_index).into()),
			UniversalOrigin(GlobalConsensus(network)),
			ReserveAssetDeposited(asset.clone().into()),
			ClearOrigin,
		];
```

**File:** bridges/snowbridge/primitives/inbound-queue/src/v1.rs (L374-381)
```rust
			None => {
				instructions.extend(vec![
					// Deposit both asset and fees to beneficiary so the fees will not get
					// trapped. Another benefit is when fees left more than ED on AssetHub could be
					// used to create the beneficiary account in case it does not exist.
					DepositAsset { assets: Wild(AllCounted(2)), beneficiary },
				]);
			},
```

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

**File:** polkadot/xcm/xcm-executor/src/lib.rs (L415-420)
```rust
			let claimer = self
				.asset_claimer
				.as_ref()
				.or(self.context.origin.as_ref())
				.unwrap_or(&self.original_origin);
			let trap_weight = Config::AssetTrap::drop_assets(claimer, self.holding, &self.context);
```

**File:** polkadot/xcm/pallet-xcm/src/lib.rs (L1527-1551)
```rust
		pub fn claim_assets(
			origin: OriginFor<T>,
			assets: Box<VersionedAssets>,
			beneficiary: Box<VersionedLocation>,
		) -> DispatchResult {
			let origin_location = T::ExecuteXcmOrigin::ensure_origin(origin)?;
			tracing::debug!(target: "xcm::pallet_xcm::claim_assets", ?origin_location, ?assets, ?beneficiary);
			// Extract version from `assets`.
			let assets_version = assets.identify_version();
			let assets: Assets = (*assets).try_into().map_err(|()| {
				tracing::debug!(
					target: "xcm::pallet_xcm::claim_assets",
					"Failed to convert input VersionedAssets",
				);
				Error::<T>::BadVersion
			})?;
			let number_of_assets = assets.len() as u32;
			let beneficiary: Location = (*beneficiary).try_into().map_err(|()| {
				tracing::debug!(
					target: "xcm::pallet_xcm::claim_assets",
					"Failed to convert beneficiary VersionedLocation",
				);
				Error::<T>::BadVersion
			})?;
			let ticket: Location = GeneralIndex(assets_version as u128).into();
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
