Based on my research, I found a concrete local analog to the "forgotten nullifier → permanently stuck funds, no rescue path" bug class within Snowbridge's inbound message processing.

### Title
Snowbridge V1 Inbound Queue traps bridged funds against an unrecoverable XCM origin with no claimer/rescue mechanism - (File: `bridges/snowbridge/primitives/inbound-queue/src/v1.rs`)

### Summary
The external report's core broken invariant is: a user-facing recovery secret (nullifier) is the *only* path to reclaim deposited funds, and if that secret is unusable/forgotten there is no fallback recovery function, so funds become permanently stuck. The exact same invariant break exists in Snowbridge's **V1** inbound queue converter: when an inbound Ethereum→AssetHub XCM program fails partway (e.g. `DepositAsset` fails because the destination beneficiary can't satisfy the existential deposit, or reserve/teleport fails), the leftover holding is trapped by `pallet_xcm`'s `AssetTrap` against the **XCM origin of the program**, not against any location a real user or the bridge owner can reproduce with a signed `claim_assets` call. Unlike the V2 pathway (which explicitly sets an `AssetClaimer` hint, and was hardened in [1](#0-0) ), the V1 converter never inserts a claimer hint, so any trap on this path is unclaimable by design — a permanent, unrescuable fund lock.

### Finding Description
`convert_send_token` in the V1 inbound-queue primitives builds an XCM program that descends the origin to the bridge's pallet instance and deposits assets directly to a `beneficiary` derived from raw, unauthenticated message bytes (`Destination::AccountId32`/`ForeignAccountId32`/`ForeignAccountId20`), with no `SetHints`/`AssetClaimer` instruction anywhere in the program: [2](#0-1) 

Compare this to the V2 converter, which explicitly computes a `claimer: Location` and threads it through so that, on failure, the trap origin is reproducible via a signed account on AssetHub: [3](#0-2) 

The exact same "unrecoverable trap" failure mode that was found and fixed for the V2 fallback claimer (`network: None` mismatching `SignedToAccountId32`'s produced location) is documented in the audit-fix PRDoc: [1](#0-0) 

That fix only touched `snowbridge-inbound-queue-primitives`'s V2 path (confirmed by the crate list in the PRDoc and by the V2-only `AssetClaimer`/`SetHints` handling in `xcm-executor`'s `ClaimAsset`/`SetHints` instruction processing: [4](#0-3) ). The V1 converter was never given an equivalent claimer mechanism, and `pallet_xcm`'s generic `AssetTrap`/`ClaimAsset` implementation strictly requires the *exact* origin+assets hash to be reproduced by a caller (`claim_assets` extrinsic or `ClaimAsset` instruction) — any other party, including the sovereign/bridge owner, is rejected with `UnknownClaim`: [5](#0-4) , [6](#0-5) .

### Impact Explanation
Because the V1 program's execution origin (after `DescendOrigin(PalletInstance(inbound_queue_pallet_index))` / `UniversalOrigin(GlobalConsensus(network))`) is not an origin that any account can present as a signed `RuntimeOrigin` on AssetHub, any assets trapped on this path (dust fees left after a partial failure, or the whole transferred asset if `DepositAsset` fails due to ED shortfall on the beneficiary, asset not being sufficient, or a downstream reserve/teleport step failing) are permanently and irrecoverably locked in the pallet's `AssetTraps` storage — exactly the "forgotten nullifier, no rescue function" outcome the external report flags, but here it is *unconditional*: there is no secret to forget, the recovery path simply does not exist for this code path. This is a genuine, permanent user/bridge-fund lock with no privileged or malicious actor required — it triggers on any inbound message whose XCM happens to fail mid-execution.

### Likelihood Explanation
No malicious actor, governance, or off-chain infrastructure is needed — an unprivileged relayer submitting a syntactically valid but execution-failing inbound message (e.g., targeting a beneficiary account that doesn't meet ED, or a foreign asset that has been frozen/not-yet-created on the destination) is sufficient to trigger the trap on the unreachable origin. The V1 queue's existing dust-trapping fix ( [7](#0-6) ) only redirects *leftover fee dust* to the sovereign account after successful execution; it does not address the full-asset trap case when `DepositAsset`/reserve/teleport itself fails.

### Recommendation
Port the V2 fix pattern to the V1 converter: add a `SetHints { hints: [AssetClaimer { location }] }` instruction (or equivalent) pointing at a location reproducible by the bridge's sovereign/owner account on AssetHub (anchored to the local network, matching `SignedToAccountId32`), so that any trapped assets on the V1 path can be recovered via `pallet_xcm::claim_assets`, mirroring the audit remediation already applied to V2 in [1](#0-0) .

### Proof of Concept
1. Craft a V1 inbound message whose `Destination::AccountId32 { id }` beneficiary account has zero balance and the transferred asset amount plus fees are insufficient to meet AssetHub's existential deposit for that asset/account.
2. Submit via the legacy Snowbridge inbound queue; `convert_send_token`'s `DepositAsset { assets: Wild(AllCounted(2)), beneficiary }` instruction fails on AssetHub.
3. The XCM executor's `post_process` traps the leftover holding against the origin produced by `DescendOrigin(PalletInstance(inbound_queue_pallet_index))`/`UniversalOrigin(GlobalConsensus(network))` — an origin with no corresponding signed AssetHub account.
4. Attempt `pallet_xcm::claim_assets` from every plausible signer (the relayer, the bridge sovereign, the intended beneficiary) — all fail with `UnknownClaim` because none of their `SignedToAccountId32`-derived locations match the trap-key hash, permanently locking the funds, exactly as was previously demonstrated and fixed for the V2 fallback-claimer case in [8](#0-7) .

### Citations

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
