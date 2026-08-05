This confirms the analog is real and verified by the test suite. The pallet's own tests explicitly cover `register_token` being blocked while halted (`test_switch_operating_mode`, `pallets/system-frontend/src/tests.rs:113-147`) but there is **no equivalent test for `add_tip`** — and reading the source confirms why: `add_tip` never checks `ExportOperatingMode`.

### Title
Halted export operating mode does not block `add_tip`, allowing users to burn/swap fee assets while Snowbridge export is paused - (`bridges/snowbridge/pallets/system-frontend/src/lib.rs`)

### Summary
The `snowbridge-pallet-system-frontend`'s `register_token` extrinsic correctly checks `ExportOperatingMode` before executing, rejecting calls with `Error::<T>::Halted` when governance has halted Ethereum message export. However the sibling extrinsic `add_tip`, which performs the equivalent user-funds-affecting operation (swapping/burning a user-supplied fee asset and dispatching an XCM `Transact` to BridgeHub), contains no such check. [1](#0-0) [2](#0-1) 

### Finding Description
`register_token` explicitly guards on the halted state:
```rust
pub fn register_token(...) -> DispatchResult {
    ensure!(!Self::export_operating_mode().is_halted(), Error::<T>::Halted);
    ...
}
``` [3](#0-2) 

`add_tip` performs no such check before calling `swap_fee_asset_and_burn`, which swaps the caller's supplied asset for Ether via `T::Swap::swap_exact_tokens_for_tokens` and then calls `burn_for_teleport` to irrevocably burn it, followed by dispatching an XCM `Transact` (`send_transact_call`) to BridgeHub to register the tip:
```rust
pub fn add_tip(origin: OriginFor<T>, message_id: MessageId, asset: Asset) -> DispatchResult {
    let who = ensure_signed(origin)?;
    let ether_gained = Self::swap_fee_asset_and_burn(who.clone().into(), asset)?;
    let call = Self::build_add_tip_call(who.clone(), message_id.clone(), ether_gained);
    Self::send_transact_call(who.into(), call)
}
``` [4](#0-3) 

`ExportOperatingMode` exists specifically to let governance halt exporting to Ethereum (e.g. after a suspected compromise), and `PausableExporter`/`ExportPausedQuery` is designed so that once halted, `SendXcm::validate`/`deliver` reject new outbound XCM (`Err(SendError::NotApplicable)`) for the *outbound message router*. But `add_tip`'s asset burn happens in `swap_fee_asset_and_burn` before any XCM send is attempted, so the fund-destructive step (swap + burn) executes unconditionally regardless of halted state, exactly mirroring the reported pattern where `addGasFee` moves user funds without checking whether the gateway is paused. This is also structurally identical to the class of bug already acknowledged and fixed for the verifier layer in `prdoc/stable2603-2/pr_11856.prdoc`, which closed a gap where halting only blocked *some* entrypoints (`EthereumBeaconClient::submit`) while `inbound_queue_v2::submit` and `outbound_queue_v2::submit_delivery_receipt` kept processing and paying rewards while the bridge was halted — the same "halt does not cover all money-moving paths" defect now still present in `add_tip`. [5](#0-4) [6](#0-5) 

Even if the XCM `Transact` send later fails or `add_tip`'s BridgeHub-side handling is a no-op while the pallet is halted downstream, the user's fee asset has already been swapped/burned upstream in the frontend pallet on AssetHub — a state-changing, irreversible action performed before any halted check.

### Impact Explanation
When governance halts Ethereum export (e.g., due to a suspected compromise of the light client, the outbound queue, or the Gateway contract on Ethereum), users can still call `add_tip`, causing their tokens to be swapped for Ether and burned for teleportation, and an XCM `Transact` dispatched toward BridgeHub. This burn happens unconditionally and is irrecoverable if the corresponding tip registration never lands (e.g., because BridgeHub-side processing is itself halted, matching the precedent in `pr_11856.prdoc`), resulting in a permanent loss of user funds during exactly the maintenance/incident window the halt mechanism is meant to protect against.

### Likelihood Explanation
Likelihood is moderate: it requires governance to have placed the bridge in `Halted` mode (an expected, periodic/incident-response operation, not attacker-controlled), after which any ordinary signed user calling the public, unprivileged `add_tip` extrinsic triggers the loss. No malicious peer, relayer, validator, or admin abuse is required — the caller is a normal user acting during a state that is supposed to freeze money-moving export operations.

### Recommendation
Add the same halted check present in `register_token` to `add_tip`, before `swap_fee_asset_and_burn` is invoked:
```rust
pub fn add_tip(origin: OriginFor<T>, message_id: MessageId, asset: Asset) -> DispatchResult {
    let who = ensure_signed(origin)?;
    ensure!(!Self::export_operating_mode().is_halted(), Error::<T>::Halted);
    let ether_gained = Self::swap_fee_asset_and_burn(who.clone().into(), asset)?;
    ...
}
```

### Proof of Concept
1. Governance calls `set_operating_mode(Root, BasicOperatingMode::Halted)` on `snowbridge-pallet-system-frontend`, as demonstrated in `test_switch_operating_mode` [7](#0-6) .
2. Attempting `register_token` now correctly fails with `Error::<T>::Halted`.
3. A user then calls `add_tip(origin, message_id, asset)` with a non-Ether asset — this succeeds (as shown by the existing passing test `add_tip_non_ether_asset_succeeds`, which has no halted-mode counterpart) [8](#0-7) , swapping and burning the user's asset even though export is halted.
4. If the halted state was set because the bridge/verifier/BridgeHub side is compromised or paused, the user's asset is burned with no guarantee the tip will ever be honored, exactly reproducing the "paused gateway does not prevent users from adding gas fees" invariant break from the external report.

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

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/exporter.rs (L19-34)
```rust
	fn validate(
		destination: &mut Option<Location>,
		message: &mut Option<Xcm<()>>,
	) -> SendResult<Self::Ticket> {
		match PausedQuery::is_paused() {
			true => Err(SendError::NotApplicable),
			false => InnerExporter::validate(destination, message),
		}
	}

	fn deliver(ticket: Self::Ticket) -> Result<XcmHash, SendError> {
		match PausedQuery::is_paused() {
			true => Err(SendError::NotApplicable),
			false => InnerExporter::deliver(ticket),
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

**File:** bridges/snowbridge/pallets/system-frontend/src/tests.rs (L176-201)
```rust
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
