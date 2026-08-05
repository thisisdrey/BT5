### Title
`add_tip` missing halted-mode check allows fund burn while Snowbridge export is paused - (File: `bridges/snowbridge/pallets/system-frontend/src/lib.rs`)

### Summary
The `snowbridge-pallet-system-frontend` implements a pause mechanism (`ExportOperatingMode`, toggled via `set_operating_mode`) that is meant to halt export-related, fund-moving operations to Ethereum. The `register_token` extrinsic correctly checks `ensure!(!Self::export_operating_mode().is_halted(), Error::<T>::Halted)` before doing any fee-asset swap/burn, but the sibling extrinsic `add_tip` — which performs the exact same swap-and-burn side effect via the shared helper `swap_fee_asset_and_burn` — omits this check entirely.

### Finding Description
`register_token` and `add_tip` both call `Self::swap_fee_asset_and_burn(...)`, which swaps the caller's supplied asset for Ether and burns it via `burn_for_teleport::<T::AssetTransactor>` (an irreversible action that removes value from the user's account and is intended to fund an Ethereum-bound XCM message). [1](#0-0) [2](#0-1) 

`register_token` guards this with the halted check:
```
ensure!(!Self::export_operating_mode().is_halted(), Error::<T>::Halted);
``` [3](#0-2) 

`add_tip` has no equivalent check — it goes straight from `ensure_signed(origin)?` to `Self::swap_fee_asset_and_burn(...)`:
```
pub fn add_tip(origin: OriginFor<T>, message_id: MessageId, asset: Asset) -> DispatchResult
{
    let who = ensure_signed(origin)?;
    let ether_gained = Self::swap_fee_asset_and_burn(who.clone().into(), asset)?;
    ...
}
``` [4](#0-3) 

The `ExportOperatingMode` storage is explicitly surfaced to the rest of the runtime as an `ExportPausedQuery` implementation, meant to signal "export to Ethereum is paused" for downstream consumers such as the outbound-queue exporter: [5](#0-4) 
`ExportPausedQuery` is consumed by `bridges/snowbridge/primitives/outbound-queue/src/v2/exporter.rs`, i.e. the halted flag is intended to be an authoritative pause signal for the whole export path, not just for `register_token`. Because `add_tip` bypasses this check, an unprivileged, signed user can continue to swap and irrecoverably burn their assets (fee/tip asset → Ether → burn) even while the pallet owner/root has explicitly halted export operations via `set_operating_mode(Halted)`, and can still enqueue a `Transact` XCM to the backend `EthereumSystem::add_tip` call on BridgeHub while the system is supposed to be frozen.

Existing tests only check the halted path for `register_token` (`test_switch_operating_mode`), never for `add_tip`, confirming this gap was not covered/considered. [6](#0-5) 

### Impact Explanation
This directly matches the "public underpriced/uncontrolled work that degrades … bridge processing" and "permanent user-fund … lock" classes in scope: halting export mode is an emergency control meant to stop irreversible burns/XCM sends during incident response (e.g., a bug in the Ethereum-side gateway, a compromised relay, or an ongoing exploit). Because `add_tip` is not gated, users can keep burning tip assets and dispatching `Transact` calls into the BridgeHub backend pallet during the halt window, defeating the purpose of the pause and continuing to move/burn funds that the operator explicitly tried to freeze. Depending on backend behavior for calls received while `EthereumSystem` itself may also be paused, this can also produce burned-but-unprocessed value (funds burned locally but the corresponding reward/tip never lands on Ethereum), i.e. fund loss for callers who to trusted the "halted" state to mean no such operations were possible.

### Likelihood Explanation
High likelihood: the missing check requires no privileged access — any signed account can call `add_tip` with an arbitrary `asset`/`message_id` at any time, including immediately after (or during) a halt. No malicious peer, validator, or governance actor is needed; the flaw is a straightforward asymmetry between two public extrinsics sharing the same fund-burning helper function.

### Recommendation
Add the same halted-mode guard used in `register_token` to `add_tip`:
```rust
pub fn add_tip(origin: OriginFor<T>, message_id: MessageId, asset: Asset) -> DispatchResult {
    let who = ensure_signed(origin)?;
    ensure!(!Self::export_operating_mode().is_halted(), Error::<T>::Halted);
    let ether_gained = Self::swap_fee_asset_and_burn(who.clone().into(), asset)?;
    ...
}
```
Consider centralizing the check inside `swap_fee_asset_and_burn` itself so any future caller of this fund-burning helper cannot forget the guard.

### Proof of Concept
1. Root/owner calls `set_operating_mode(Halted)` to freeze Snowbridge export operations (as demonstrated in `test_switch_operating_mode`). [6](#0-5) 
2. Verify `register_token` is correctly rejected with `Error::<T>::Halted` (already covered by the existing test).
3. Call `EthereumSystemFrontend::add_tip(RuntimeOrigin::signed(who), message_id, asset)` with a valid `asset` (e.g., same setup as `add_tip_ether_asset_succeeds` / `add_tip_non_ether_asset_succeeds`). [7](#0-6) 
4. Observe the call succeeds and emits `Event::MessageSent`, and the tip asset is swapped/burned — despite the pallet being in `Halted` mode, demonstrating the missing guard.

### Citations

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

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L254-273)
```rust
		/// Add an additional relayer tip for a committed message identified by `message_id`.
		/// The tip asset will be swapped for ether.
		#[pallet::call_index(2)]
		#[pallet::weight(
			T::WeightInfo::add_tip()
				.saturating_add(T::BackendWeightInfo::transact_add_tip())
		)]
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
