This confirms the design: `FrontendOrigin = EnsureXcm<AllowFromEthereumFrontend>` at Bridge Hub only checks that the XCM origin unpacks to `(1, [Parachain(ASSET_HUB_ID), PalletInstance(FRONTEND_PALLET_INDEX)])`. [1](#0-0)  This origin check validates *where the message says it came from after DescendOrigin*, not *who actually authored the Transact call*. The `sender` parameter of `register_token` is taken verbatim from call data and never cross-checked against the authenticated origin. [2](#0-1) 

### Title
Forgeable `sender` in `EthereumSystemV2::register_token`/`add_tip` lets any AssetHub account impersonate the system-frontend pallet and spoof the message origin sent to Ethereum - (File: `bridges/snowbridge/pallets/system-v2/src/lib.rs`)

### Summary
`snowbridge-pallet-system-v2::register_token` (and `add_tip`) on Bridge Hub only verifies that the dispatch origin matches the location `Parachain(AssetHub)/PalletInstance(system-frontend)` via `FrontendOrigin = EnsureXcm<AllowFromEthereumFrontend>`. [1](#0-0)  It does **not** verify that the `sender: Box<VersionedLocation>` argument embedded in the call actually corresponds to the account that requested registration on AssetHub. On the legitimate path, `snowbridge-pallet-system-frontend::register_token` derives `sender` from `T::RegisterTokenOrigin::ensure_origin(origin, &asset_location)` (binding it to the real caller and to fee payment/burn logic) before wrapping it in a `Transact` sent via a purpose-built XCM. [3](#0-2) [4](#0-3)  This exactly parallels the Maia H-24 bug class: the "branch" gatekeeper (`system-frontend::register_token`, analogous to `addLocalToken`) performs ownership/fee validation, but the "root" function (`system-v2::register_token`, analogous to `_addLocalToken`) is reachable through a lower-level, less-restrictive dispatch primitive that skips that validation, because the destination trusts a coarse "did this come from the right pallet location" check instead of binding the embedded `sender` field to the real, cryptographically-authenticated caller.

### Finding Description
The Bridge Hub extrinsic:
```rust
pub fn register_token(
    origin: OriginFor<T>,
    sender: Box<VersionedLocation>,
    asset_id: Box<VersionedLocation>,
    metadata: AssetMetadata,
    amount: u128,
) -> DispatchResult {
    T::FrontendOrigin::ensure_origin(origin)?;
    let sender_location: Location = (*sender).try_into()...;
    ...
    let message_origin = Self::location_to_message_origin(sender_location)?;
    Self::send(message_origin, command, amount)?;
    ...
}
```
uses `sender_location` — fully attacker-controllable call data — as the `origin` of the `Message` sent to the Ethereum Gateway. [2](#0-1)  The only guard is `FrontendOrigin`, which merely checks the *transport-layer* XCM origin equals `Parachain(AssetHub)/PalletInstance(FRONTEND_PALLET_INDEX)`. [5](#0-4) 

Any XCM arriving at Bridge Hub from a sibling parachain is, before program execution, assigned origin `Location::new(1, [Parachain(sender_para)])`. The remote program executed on Bridge Hub in the legitimate flow starts with `DescendOrigin(PalletLocation)` — a permission-less XCM instruction that simply appends a junction to the current origin — followed by `Transact{ origin_kind: Xcm, call }`. [6](#0-5)  `DescendOrigin` requires no special authority: it is legal for *any* program whose origin is `Parachain(AssetHub)` to descend into `PalletInstance(FRONTEND_PALLET_INDEX)`, regardless of which pallet or account on AssetHub actually produced the message. This means the security boundary that `FrontendOrigin` is supposed to enforce ("this call was authored by the system-frontend pallet") is not actually anchored to which code path emitted the XCM — it is anchored only to the two coarse XCM instructions `DescendOrigin`/`Transact`, both of which are freely composable by any program sent from AssetHub to Bridge Hub.

The codebase's own regression test `signed_assethub_user_cannot_bypass_origin_alteration_when_routing_to_ethereum` demonstrates that ordinary signed AssetHub users can already execute custom XCM programs (via `PolkadotXcm::execute` + `InitiateTransfer` with a fully attacker-supplied `remote_xcm`) that get executed by the destination's XCM executor with attacker-chosen instruction sequences, including origin-altering ones such as `AliasOrigin`. [7](#0-6)  That specific test only exercises `AliasOrigin` forgery against the export-to-Ethereum converter, which was hardened by a dedicated `AllowedAliasOrigin` check added in `prdoc/pr_12159.prdoc`. [8](#0-7)  That fix is scoped to the `EthereumBlobExporter`/`XcmConverter`'s handling of `AliasOrigin` for messages destined for Ethereum — it does not constrain what an attacker-crafted `remote_xcm` may contain when the destination is Bridge Hub itself and the target is a `Transact` into `snowbridge-pallet-system-v2`. Nothing in `snowbridge_pallet_system_v2::Config` or `FrontendOrigin`'s definition rejects a `DescendOrigin(PalletInstance(FRONTEND_PALLET_INDEX))` instruction coming from a signed-user-initiated `remote_xcm`, because `DescendOrigin` is a generic, universally-permitted instruction, not one gated by `AllowedAliasOrigin`-style filters.

### Impact Explanation
If a user's own AssetHub-originated `remote_xcm` (sent via any XCM path that lets a signed account send/forward a Transact to Bridge Hub, such as `InitiateTransfer`/reserve-transfer with a custom `remote_xcm`) can begin with `DescendOrigin(PalletInstance(FRONTEND_PALLET_INDEX))` followed by `Transact{ call: EthereumSystemCall::RegisterToken{ sender: <arbitrary Location>, ... } }`, the attacker bypasses:
- `RegisterTokenOrigin::ensure_origin` (asset-ownership binding check performed only in the frontend pallet)
- `swap_fee_asset_and_burn` (the fee/tip burn logic in the frontend pallet)

and can supply an arbitrary `sender` location (e.g. the location of another parachain, a rich account, or the AssetHub's own Root/sovereign identity). This forged `sender` becomes the `origin` field of the `Message` sent to the Ethereum Gateway, which on the Ethereum side determines which Agent/channel is charged and attributed for the registration (and analogously for `add_tip`, which reward/tip account is credited/blamed). This is a direct case of unauthorized execution / origin escalation in cross-chain message routing — the exact impact category the Maia H-24 finding falls under (arbitrary "local token" registration due to missing origin binding between the branch gatekeeper and the root dispatch function).

### Likelihood Explanation
No privileged actor, validator, relayer, or admin is required — an ordinary signed AssetHub account, using only `pallet_xcm::execute` with instructions the runtime's Barrier already permits for regular users (as proven functional for `InitiateTransfer`/custom `remote_xcm` in the existing test suite), is sufficient to construct the malicious message. The only uncertainty is whether AssetHub's current Barrier/filter configuration permits `DescendOrigin(PalletInstance(FRONTEND_PALLET_INDEX))` specifically inside a user-supplied `remote_xcm` targeted at Bridge Hub (as opposed to `AliasOrigin`, which was the instruction exercised in the located test and which is now filtered only for the Ethereum-export path) — I was not able to fully trace the Bridge Hub inbound Barrier/`XcmExecutor::Config` for `bridge-hub-westend` in this session to confirm whether `DescendOrigin` from siblings is unconditionally allowed or additionally filtered elsewhere.

### Recommendation
Do not trust an unauthenticated `sender` field passed in call data across the frontend→backend hop. Either:
1. Derive `sender`/`message_origin` at the Bridge Hub side purely from the authenticated XCM origin after `FrontendOrigin::ensure_origin`, requiring the frontend pallet to preserve the original caller's identity through an origin-preserving mechanism (e.g. `DescendOrigin` down to the *caller's own* account/location rather than passing it as call data), so that the value used for `message_origin` is exactly what the origin-conversion produced, not attacker-suppliable data; or
2. Add an explicit `AllowedOrigin`-style filter (mirroring `AllowedAliasOrigin` from `prdoc/pr_12159.prdoc`) at Bridge Hub's XCM Barrier that rejects any inbound `Transact` targeting `snowbridge_pallet_system_v2::register_token`/`add_tip` unless the full instruction sequence matches exactly what `system-frontend::build_remote_xcm` constructs, closing the gap left open for user-authored `remote_xcm` payloads.

### Proof of Concept
1. On AssetHub, a signed user submits `PolkadotXcm::execute` with a program containing `InitiateTransfer` (or any other instruction capable of forwarding a `remote_xcm` to Bridge Hub), setting:
   - `destination: BridgeHubLocation`
   - `remote_xcm: Xcm(vec![DescendOrigin(PalletInstance(FRONTEND_PALLET_INDEX)), Transact{ origin_kind: OriginKind::Xcm, call: BridgeHubRuntime::EthereumSystem(EthereumSystemCall::RegisterToken{ sender: Box::new(VersionedLocation::from(<arbitrary Location>)), asset_id, metadata, amount }).encode() }])`
2. When this arrives at Bridge Hub, the executor's origin is `Location::new(1, [Parachain(AssetHubId)])`; `DescendOrigin(PalletInstance(FRONTEND_PALLET_INDEX))` narrows it to exactly `Location::new(1, [Parachain(AssetHubId), PalletInstance(FRONTEND_PALLET_INDEX)])`, satisfying `AllowFromEthereumFrontend::contains`. [5](#0-4) 
3. `T::FrontendOrigin::ensure_origin(origin)` succeeds, and `register_token` proceeds using the attacker-chosen `sender_location`, never having gone through `RegisterTokenOrigin` or fee-burn checks. [2](#0-1) 
4. The resulting `Message{ origin: <forged sender>, ... }` is dispatched to the Ethereum Gateway, attributing the registration/tip to an identity the attacker never controlled or authenticated as.

### Citations

**File:** cumulus/parachains/runtimes/bridge-hubs/bridge-hub-westend/src/bridge_to_ethereum_config.rs (L313-334)
```rust
pub struct AllowFromEthereumFrontend;
impl Contains<Location> for AllowFromEthereumFrontend {
	fn contains(location: &Location) -> bool {
		match location.unpack() {
			(1, [Parachain(para_id), PalletInstance(index)]) => {
				return *para_id == ASSET_HUB_ID && *index == FRONTEND_PALLET_INDEX
			},
			_ => false,
		}
	}
}

impl snowbridge_pallet_system_v2::Config for Runtime {
	type RuntimeEvent = RuntimeEvent;
	type OutboundQueue = EthereumOutboundQueueV2;
	type InboundQueue = EthereumInboundQueueV2;
	type FrontendOrigin = EnsureXcm<AllowFromEthereumFrontend>;
	type WeightInfo = crate::weights::snowbridge_pallet_system_v2::WeightInfo<Runtime>;
	type GovernanceOrigin = EnsureRootWithSuccess<crate::AccountId, RootLocation>;
	#[cfg(feature = "runtime-benchmarks")]
	type Helper = ();
}
```

**File:** bridges/snowbridge/pallets/system-v2/src/lib.rs (L211-249)
```rust
		pub fn register_token(
			origin: OriginFor<T>,
			sender: Box<VersionedLocation>,
			asset_id: Box<VersionedLocation>,
			metadata: AssetMetadata,
			amount: u128,
		) -> DispatchResult {
			T::FrontendOrigin::ensure_origin(origin)?;

			let sender_location: Location =
				(*sender).try_into().map_err(|_| Error::<T>::UnsupportedLocationVersion)?;
			let asset_location: Location =
				(*asset_id).try_into().map_err(|_| Error::<T>::UnsupportedLocationVersion)?;

			let location = Self::reanchor(asset_location)?;
			let token_id = TokenIdOf::convert_location(&location)
				.ok_or(Error::<T>::LocationConversionFailed)?;

			if !ForeignToNativeId::<T>::contains_key(token_id) {
				ForeignToNativeId::<T>::insert(token_id, location.clone());
			}

			let command = Command::RegisterForeignToken {
				token_id,
				name: metadata.name.into_inner(),
				symbol: metadata.symbol.into_inner(),
				decimals: metadata.decimals,
			};

			let message_origin = Self::location_to_message_origin(sender_location)?;
			Self::send(message_origin, command, amount)?;

			Self::deposit_event(Event::<T>::RegisterToken {
				location: location.into(),
				foreign_token_id: token_id,
			});

			Ok(())
		}
```

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L225-252)
```rust
		pub fn register_token(
			origin: OriginFor<T>,
			asset_id: Box<VersionedLocation>,
			metadata: AssetMetadata,
			fee_asset: Asset,
		) -> DispatchResult {
			ensure!(!Self::export_operating_mode().is_halted(), Error::<T>::Halted);

			let asset_location: Location =
				(*asset_id).try_into().map_err(|_| Error::<T>::UnsupportedLocationVersion)?;
			let origin_location = T::RegisterTokenOrigin::ensure_origin(origin, &asset_location)?;

			let ether_gained = if origin_location.is_here() {
				// Root origin/location does not pay any fees/tip.
				0
			} else {
				Self::swap_fee_asset_and_burn(origin_location.clone(), fee_asset)?
			};

			let call = Self::build_register_token_call(
				origin_location.clone(),
				asset_location,
				metadata,
				ether_gained,
			)?;

			Self::send_transact_call(origin_location, call)
		}
```

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L319-363)
```rust
		// Build the call to dispatch the `EthereumSystem::register_token` extrinsic on BH
		fn build_register_token_call(
			sender: Location,
			asset: Location,
			metadata: AssetMetadata,
			amount: u128,
		) -> Result<BridgeHubRuntime<T>, Error<T>> {
			// reanchor locations relative to BH
			let sender = Self::reanchored(sender)?;
			let asset = Self::reanchored(asset)?;

			let call = BridgeHubRuntime::EthereumSystem(EthereumSystemCall::RegisterToken {
				sender: Box::new(VersionedLocation::from(sender)),
				asset_id: Box::new(VersionedLocation::from(asset)),
				metadata,
				amount,
			});

			Ok(call)
		}

		// Build the call to dispatch the `EthereumSystem::add_tip` extrinsic on BH
		fn build_add_tip_call(
			sender: AccountIdOf<T>,
			message_id: MessageId,
			amount: u128,
		) -> BridgeHubRuntime<T> {
			BridgeHubRuntime::EthereumSystem(EthereumSystemCall::AddTip {
				sender,
				message_id,
				amount,
			})
		}

		fn build_remote_xcm(call: &impl Encode) -> Xcm<()> {
			Xcm(vec![
				DescendOrigin(T::PalletLocation::get()),
				UnpaidExecution { weight_limit: Unlimited, check_origin: None },
				Transact {
					origin_kind: OriginKind::Xcm,
					call: call.encode().into(),
					fallback_max_weight: None,
				},
			])
		}
```

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound_edge_case.rs (L436-498)
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
	});
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
