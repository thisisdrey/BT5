Audit Report

## Title
`add_tip` in `SnowbridgeSystemFrontend` bypasses the pause/halt guard, letting users burn real value while message export is supposed to be halted - (File: `bridges/snowbridge/pallets/system-frontend/src/lib.rs`)

## Summary
`ExportOperatingMode<T>` is the pallet's sole halt flag, consulted correctly by `register_token` via `ensure!(!Self::export_operating_mode().is_halted(), Error::<T>::Halted)` before any state change, but `add_tip` performs its asset swap, ether burn, and cross-chain `Transact` dispatch without ever checking this flag. This lets any signed user burn assets and push a BridgeHub `Transact` while governance has explicitly halted Ethereum export.

## Finding Description
`register_token` checks the halt flag as its very first statement: [1](#0-0) . `add_tip`, however, goes straight from `ensure_signed` into `swap_fee_asset_and_burn` and `send_transact_call` with no equivalent check: [2](#0-1) . `ExportPausedQuery::is_paused()` reads exactly this same flag: [3](#0-2) . The BridgeHub-side `system-v2::add_tip` also performs no operating-mode check, forwarding straight to `InboundQueue::add_tip`/`OutboundQueue::add_tip`: [4](#0-3) . Unit tests confirm the asymmetry: `test_switch_operating_mode` verifies `register_token` is rejected with `Error::Halted` once halted [5](#0-4) , while no analogous test exists (or could pass) for `add_tip`, and `add_tip_ether_asset_succeeds`/`add_tip_non_ether_asset_succeeds` show it always succeeds when there is no halt-state test at all [6](#0-5) .

This is consistent with a documented pattern the codebase itself already treats as a security gap elsewhere: the `pallet-ethereum-client` `Verifier::verify` implementation was patched specifically because halting the light client "only blocked new beacon header updates... Proof verification still ran, which meant `inbound_queue_v2::submit` and `outbound_queue_v2::submit_delivery_receipt` could continue to process receipts and pay out relayer rewards... while governance had halted the bridge" [7](#0-6) . `add_tip` in `system-frontend` exhibits the same class of gap: value-moving logic (swap + burn + cross-chain dispatch) that is reachable and executes fully while the halt flag is set, because the guard was applied to only one of the two public dispatchables.

## Impact Explanation
When `ExportOperatingMode` is set to `Halted` — the mechanism intended to stop all Ethereum-export activity during an incident — an unprivileged signed user can still call `add_tip`, causing a real swap of their tip asset into Ether, an ether burn via `burn_for_teleport`, and an XCM `Transact` dispatch toward BridgeHub's `EthereumSystemCall::AddTip`. This keeps injecting cross-chain traffic and consuming real backing value while the pallet's control-plane is supposed to be fully stopped, undermining the operator's ability to fully halt bridge activity during an emergency — falling under "public underpriced work that degrades... stalls bridge processing" in the impact gate, since the halt invariant is not uniformly enforced across all value-moving entry points.

## Likelihood Explanation
High likelihood. `add_tip` is an ordinary signed extrinsic requiring no special privilege, and the omission is a straightforward absence of the same one-line guard present in the sibling `register_token` function. Any account holding a swappable tip asset can trigger this at any time the pallet is halted, with no race condition or governance action needed.

## Recommendation
Add `ensure!(!Self::export_operating_mode().is_halted(), Error::<T>::Halted);` as the first statement in `add_tip` in `bridges/snowbridge/pallets/system-frontend/src/lib.rs`, mirroring `register_token`. Additionally review `snowbridge-pallet-system-v2::add_tip` in `bridges/snowbridge/pallets/system-v2/src/lib.rs` to ensure it (or its downstream `InboundQueue`/`OutboundQueue::add_tip` calls) also consult the relevant `OperatingMode`/halted flag before mutating tip/reward state.

## Proof of Concept
1. Root calls `SnowbridgeSystemFrontend::set_operating_mode(Halted)`, setting `ExportOperatingMode::Halted`.
2. Call `SnowbridgeSystemFrontend::register_token(...)` — fails with `Error::Halted` as shown in `test_switch_operating_mode` [8](#0-7) .
3. Call `SnowbridgeSystemFrontend::add_tip(origin, message_id, asset)` with a valid tip asset in the same halted state — the extrinsic succeeds exactly as in `add_tip_ether_asset_succeeds`/`add_tip_non_ether_asset_succeeds` [6](#0-5) , performing the swap/burn and emitting `MessageSent`, confirming the halt guard is bypassed for this entry point.

### Citations

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L230-231)
```rust
		) -> DispatchResult {
			ensure!(!Self::export_operating_mode().is_halted(), Error::<T>::Halted);
```

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L261-273)
```rust
		pub fn add_tip(origin: OriginFor<T>, message_id: MessageId, asset: Asset) -> DispatchResult
		where
			<T as frame_system::Config>::AccountId: Into<Location>,
		{
			let who = ensure_signed(origin)?;

			let ether_gained = Self::swap_fee_asset_and_burn(who.clone().into(), asset)?;

			// Send the tip details to BH to be allocated to the reward in the Inbound/Outbound
			// pallet
			let call = Self::build_add_tip_call(who.clone(), message_id.clone(), ether_gained);
			Self::send_transact_call(who.into(), call)
		}
```

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L426-430)
```rust
	impl<T: Config> ExportPausedQuery for Pallet<T> {
		fn is_paused() -> bool {
			Self::export_operating_mode().is_halted()
		}
	}
```

**File:** bridges/snowbridge/pallets/system-v2/src/lib.rs (L251-264)
```rust
		#[pallet::call_index(3)]
		#[pallet::weight(<T as pallet::Config>::WeightInfo::add_tip())]
		pub fn add_tip(
			origin: OriginFor<T>,
			sender: AccountIdOf<T>,
			message_id: MessageId,
			amount: u128,
		) -> DispatchResult {
			T::FrontendOrigin::ensure_origin(origin)?;

			let result = match message_id {
				Inbound(nonce) => <T as pallet::Config>::InboundQueue::add_tip(nonce, amount),
				Outbound(nonce) => <T as pallet::Config>::OutboundQueue::add_tip(nonce, amount),
			};
```

**File:** bridges/snowbridge/pallets/system-frontend/src/tests.rs (L113-147)
```rust
#[test]
fn test_switch_operating_mode() {
	new_test_ext().execute_with(|| {
		assert_ok!(EthereumSystemFrontend::set_operating_mode(
			RawOrigin::Root.into(),
			BasicOperatingMode::Halted,
		));
		let origin_location = Location::new(1, [Parachain(2000)]);
		let origin = make_xcm_origin(origin_location);
		let asset_location: Location = Location::new(1, [Parachain(2000), GeneralIndex(1)]);
		let asset_id = Box::new(VersionedLocation::from(asset_location));
		let asset_metadata = AssetMetadata {
			name: "pal".as_bytes().to_vec().try_into().unwrap(),
			symbol: "pal".as_bytes().to_vec().try_into().unwrap(),
			decimals: 12,
		};
		let ether_location = Ether::get();
		let fee_amount = 1000;
		let asset = Asset::from((ether_location.clone(), fee_amount));
		assert_noop!(
			EthereumSystemFrontend::register_token(
				origin.clone(),
				asset_id.clone(),
				asset_metadata.clone(),
				asset.clone(),
			),
			crate::Error::<Test>::Halted
		);
		assert_ok!(EthereumSystemFrontend::set_operating_mode(
			RawOrigin::Root.into(),
			BasicOperatingMode::Normal,
		));
		assert_ok!(EthereumSystemFrontend::register_token(origin, asset_id, asset_metadata, asset));
	});
}
```

**File:** bridges/snowbridge/pallets/system-frontend/src/tests.rs (L149-201)
```rust
#[test]
fn add_tip_ether_asset_succeeds() {
	new_test_ext().execute_with(|| {
		let who: AccountId = Keyring::Alice.into();
		let message_id = MessageId::Inbound(1);
		let ether_location = Ether::get();
		let tip_amount = 1000;
		let asset = Asset::from((ether_location.clone(), tip_amount));

		assert_ok!(EthereumSystemFrontend::add_tip(
			RuntimeOrigin::signed(who.clone()),
			message_id.clone(),
			asset.clone()
		));

		let events = System::events();
		let event_record = events.last().expect("Expected at least one event").event.clone();

		if !matches!(
			event_record,
			RuntimeEvent::EthereumSystemFrontend(crate::Event::MessageSent { .. })
		) {
			panic!("Expected MessageSent event, got: {:?}", event_record);
		}
	});
}

#[test]
fn add_tip_non_ether_asset_succeeds() {
	new_test_ext().execute_with(|| {
		let who: AccountId = Keyring::Alice.into();
		let message_id = MessageId::Outbound(2);
		let non_ether_location = Location::new(1, [Parachain(3000)]);
		let tip_amount = 2000;
		let asset = Asset::from((non_ether_location.clone(), tip_amount));

		assert_ok!(EthereumSystemFrontend::add_tip(
			RuntimeOrigin::signed(who.clone()),
			message_id.clone(),
			asset.clone()
		));

		let events = System::events();
		let event_record = events.last().expect("Expected at least one event").event.clone();

		if !matches!(
			event_record,
			RuntimeEvent::EthereumSystemFrontend(crate::Event::MessageSent { .. })
		) {
			panic!("Expected MessageSent event, got: {:?}", event_record);
		}
	});
}
```

**File:** bridges/snowbridge/pallets/ethereum-client/src/impls.rs (L21-29)
```rust
	fn verify(event_log: &Log, proof: &Proof) -> Result<(), VerificationError> {
		// Refuse to verify any Ethereum-side proof while the beacon light client is halted.
		// Governance halts the light client when it suspects a compromise (e.g. sync committee
		// takeover), at which point any signed headers/receipts must be treated as untrusted.
		// Covers every Verifier consumer, including `inbound_queue_v2::submit` and
		// `outbound_queue_v2::submit_delivery_receipt` (which would otherwise still drain
		// pending relayer rewards while the bridge is halted).
		ensure!(!Self::operating_mode().is_halted(), VerificationError::Halted);

```
