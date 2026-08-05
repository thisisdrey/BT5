Audit Report

## Title
Snowbridge V1 Inbound Queue traps bridged funds against an unrecoverable XCM origin with no claimer/rescue mechanism - (File: `bridges/snowbridge/primitives/inbound-queue/src/v1.rs`)

## Summary
`convert_send_token` in the V1 inbound-queue primitives builds an XCM program that descends/aliases the origin via `DescendOrigin(PalletInstance(inbound_queue_pallet_index))` and `UniversalOrigin(GlobalConsensus(network))`, then clears it with `ClearOrigin`, before executing `DepositAsset`/`DepositReserveAsset` against a beneficiary derived from raw, unauthenticated message bytes, with no `SetHints`/`AssetClaimer` instruction anywhere in the program. [1](#0-0)  If that `DepositAsset`/`DepositReserveAsset` step fails (e.g. beneficiary can't satisfy existential deposit, asset not sufficient, or a downstream reserve/teleport failure), the leftover holding is trapped by `pallet_xcm`'s generic `AssetTrap` against the mutated/cleared execution origin rather than any location a real signer can reproduce, and there is no fallback claimer to recover it.

## Finding Description
The V1 converter never inserts an `AssetClaimer` hint, unlike the V2 converter, which explicitly computes a `claimer: Location` (falling back to the bridge's sovereign account on AssetHub) and threads it through the program. [2](#0-1)  That V2 fallback claimer previously had a bug — using `network: None` instead of `network: Some(LocalNetwork::get())` — which was found and fixed specifically because it made the trap unreachable via `SignedToAccountId32`, confirming that origin-matching precision is a real, previously-exploited failure mode in this exact bridge subsystem. [3](#0-2)  The `SetHints`/`ClaimAsset` handling in the XCM executor confirms that only an explicit `AssetClaimer` hint changes the claim/trap behavior — no such hint exists on the V1 path. [4](#0-3)  `pallet_xcm`'s `claim_assets` extrinsic and its `ClaimAssets` implementation require the caller to reproduce the *exact* trapped origin+assets hash; any mismatch is rejected with `UnknownClaim`. [5](#0-4) [6](#0-5)  The prior dust-trapping fix for V1 only redirects leftover fee dust to the sovereign account *after successful* execution via the appendix `DepositAsset` to `bridge_location`; it does not address the case where the primary `DepositAsset`/`DepositReserveAsset` instruction itself fails and the whole transferred asset (not just dust) ends up trapped. [7](#0-6) [1](#0-0) 

## Impact Explanation
This matches the "permanent user-fund or bridge-state lock" impact category: assets transferred from Ethereum through the legacy (V1) Snowbridge inbound queue can become permanently and irrecoverably locked in `pallet_xcm`'s `AssetTraps` storage on AssetHub if the deposit/reserve step fails, since no reachable claimer location exists for this program and no privileged or malicious actor is required to trigger it.

## Likelihood Explanation
An unprivileged relayer submitting a syntactically valid but execution-failing inbound message (e.g. targeting a beneficiary account that cannot meet the existential deposit, or a foreign asset not yet sufficient/created on the destination) is enough to trigger the trap on an origin that cannot be reproduced by any signed account, as demonstrated by the analogous, previously-fixed V2 defect. [8](#0-7) 

## Recommendation
Port the V2 fix pattern to the V1 converter: insert a `SetHints { hints: [AssetClaimer { location }] }` instruction pointing at a location reproducible by the bridge's sovereign/owner account on AssetHub (anchored to the local network, matching `SignedToAccountId32`), so any assets trapped on the V1 path become recoverable via `pallet_xcm::claim_assets`, mirroring the remediation already applied to V2.

## Proof of Concept
1. Craft a V1 inbound message whose `Destination::AccountId32 { id }` beneficiary has zero balance, insufficient to meet AssetHub's existential deposit for the transferred asset.
2. Submit via the legacy Snowbridge inbound queue; `convert_send_token`'s `DepositAsset { assets: Wild(AllCounted(2)), beneficiary }` fails on AssetHub.
3. The XCM executor traps the leftover holding against the origin left after `DescendOrigin`/`UniversalOrigin`/`ClearOrigin` — an origin with no corresponding signed AssetHub account.
4. Attempt `pallet_xcm::claim_assets` from the relayer, the bridge sovereign, and the intended beneficiary — all fail with `UnknownClaim`, permanently locking the funds.

### Citations

**File:** bridges/snowbridge/primitives/inbound-queue/src/v1.rs (L335-382)
```rust
		let mut instructions = vec![
			ReceiveTeleportedAsset(total_fee_asset.into()),
			BuyExecution { fees: asset_hub_fee_asset, weight_limit: Unlimited },
			DescendOrigin(PalletInstance(inbound_queue_pallet_index).into()),
			UniversalOrigin(GlobalConsensus(network)),
			ReserveAssetDeposited(asset.clone().into()),
			ClearOrigin,
		];

		match dest_para_id {
			Some(dest_para_id) => {
				let dest_para_fee_asset: Asset = (Location::parent(), dest_para_fee).into();
				let bridge_location = Location::new(2, GlobalConsensus(network));

				instructions.extend(vec![
					// After program finishes deposit any leftover assets to the snowbridge
					// sovereign.
					SetAppendix(Xcm(vec![DepositAsset {
						assets: Wild(AllCounted(2)),
						beneficiary: bridge_location,
					}])),
					// Perform a deposit reserve to send to destination chain.
					DepositReserveAsset {
						// Send over assets and unspent fees, XCM delivery fee will be charged from
						// here.
						assets: Wild(AllCounted(2)),
						dest: Location::new(1, [Parachain(dest_para_id)]),
						xcm: vec![
							// Buy execution on target.
							BuyExecution { fees: dest_para_fee_asset, weight_limit: Unlimited },
							// Deposit assets to beneficiary.
							DepositAsset { assets: Wild(AllCounted(2)), beneficiary },
							// Forward message id to destination parachain.
							SetTopic(message_id.into()),
						]
						.into(),
					},
				]);
			},
			None => {
				instructions.extend(vec![
					// Deposit both asset and fees to beneficiary so the fees will not get
					// trapped. Another benefit is when fees left more than ED on AssetHub could be
					// used to create the beneficiary account in case it does not exist.
					DepositAsset { assets: Wild(AllCounted(2)), beneficiary },
				]);
			},
		}
```

**File:** bridges/snowbridge/primitives/inbound-queue/src/v2/converter.rs (L219-229)
```rust
	/// Get sovereign account of Ethereum on Asset Hub.
	fn bridge_owner() -> Result<AccountId, ConvertMessageError> {
		let account =
			ExternalConsensusLocationsConverterFor::<
				AssetHubUniversal<LocalNetwork, AssetHubParaId>,
				AccountId,
			>::convert_location(&Location::new(2, [GlobalConsensus(EthereumNetwork::get())]))
			.ok_or(ConvertMessageError::CannotReanchor)?;

		Ok(account)
	}
```

**File:** prdoc/stable2603-3/pr_11919.prdoc (L1-16)
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
```

**File:** polkadot/xcm/xcm-executor/src/lib.rs (L1548-1564)
```rust
			SetHints { hints } => {
				for hint in hints.into_iter() {
					match hint {
						AssetClaimer { location } => {
							self.asset_claimer = Some(location)
						},
					}
				}
				Ok(())
			},
			ClaimAsset { assets, ticket } => {
				let origin = self.origin_ref().ok_or(XcmError::BadOrigin)?;
				self.ensure_can_subsume_assets(assets.len())?;
				let claimed = Config::AssetTrap::claim_assets(origin, &ticket, &assets, &self.context);
				self.holding.subsume_assets(claimed.ok_or(XcmError::UnknownClaim)?);
				Ok(())
			},
```

**File:** polkadot/xcm/pallet-xcm/src/lib.rs (L1520-1532)
```rust
		/// Claims assets trapped on this pallet because of leftover assets during XCM execution.
		///
		/// - `origin`: Anyone can call this extrinsic.
		/// - `assets`: The exact assets that were trapped. Use the version to specify what version
		/// was the latest when they were trapped.
		/// - `beneficiary`: The location/account where the claimed assets will be deposited.
		#[pallet::call_index(12)]
		pub fn claim_assets(
			origin: OriginFor<T>,
			assets: Box<VersionedAssets>,
			beneficiary: Box<VersionedLocation>,
		) -> DispatchResult {
			let origin_location = T::ExecuteXcmOrigin::ensure_origin(origin)?;
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

**File:** prdoc/1.16.0/pr_5563.prdoc (L1-13)
```text
title: "snowbridge: improve destination fee handling to avoid trapping fees dust"

doc:
  - audience: Runtime User
    description: |
      On Ethereum -> Polkadot Asset Hub messages, whether they are a token transfer
      or a `Transact` for registering a new token, any unspent fees are deposited to
      Snowbridge's sovereign account on Asset Hub, rather than trapped in AH's asset trap.

crates:
  - name: snowbridge-router-primitives
    bump: patch
  - name: snowbridge-pallet-inbound-queue
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
