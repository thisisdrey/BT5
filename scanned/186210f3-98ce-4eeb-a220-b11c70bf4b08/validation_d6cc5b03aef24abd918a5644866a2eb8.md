## Analog Found: Attacker-Controlled `decimals` in Snowbridge `register_token` Bakes a Permanent Value-Scale Mismatch into the Wrapped ERC20 on Ethereum

### Title
Unvalidated caller-supplied `decimals` in `snowbridge-pallet-system-frontend::register_token` permanently mis-scales the wrapped ERC20 minted on Ethereum - (File: `bridges/snowbridge/pallets/system-frontend/src/lib.rs`)

### Summary
The Snowbridge V2 token-registration flow lets any signed account that merely owns the referenced asset `Location` supply an arbitrary `AssetMetadata { name, symbol, decimals }` when registering a Polkadot-native asset as a wrapped ERC20 on Ethereum. Nothing in the flow cross-checks `metadata.decimals` against the actual on-chain decimals of the underlying asset (e.g. via `pallet_assets`/`pallet_balances` metadata). The value is forwarded unchanged all the way to the `RegisterForeignToken` command sent to the Ethereum Gateway, which deploys an immutable ERC20 contract using that decimals value — this is the exact "wrong decimals baked into a token representation" bug class described in the ckrBTC report, but here it is attacker-triggerable rather than a developer typo.

### Finding Description
`register_token` in `bridges/snowbridge/pallets/system-frontend/src/lib.rs` is callable by any origin (not just Root/governance): [1](#0-0) 

It only checks `T::RegisterTokenOrigin::ensure_origin(origin, &asset_location)` — i.e. that the caller controls/owns the asset's `Location` — but performs no validation of the `metadata` argument's `decimals` field against the asset's real registered decimals: [2](#0-1) 

The metadata is passed unmodified into `build_register_token_call`, which is XCM-`Transact`-ed to the backend pallet on Bridge Hub: [3](#0-2) 

On Bridge Hub, `snowbridge-pallet-system-v2::register_token` again forwards `metadata.decimals` verbatim into `Command::RegisterForeignToken`, which is what actually instructs the Ethereum Gateway to deploy the wrapped ERC20 contract: [4](#0-3) 

The integration test `register_usdt_from_owner_on_asset_hub` demonstrates this is reachable by a plain signed asset owner (not root/governance), supplying `decimals: 6` freely for USDT: [5](#0-4) 

And `register_usdt_not_from_owner_on_asset_hub_will_fail` confirms the *only* guard is asset ownership of the location — not correctness of the metadata content: [6](#0-5) 

Since ERC20 decimals are immutable once deployed on Ethereum, and the raw integer amount transferred across the bridge does not change based on the registered decimals, any mismatch between the registered `decimals` and the real decimals of the reserve asset on the Polkadot side causes a permanent `10^n` display/value-interpretation error for every unit of that wrapped token on Ethereum, for as long as the token exists.

### Impact Explanation
This directly maps to the "Incorrect Decimals" bug class from the external report, but is worse because it is not a static configuration bug — it is a value an unprivileged, non-governance caller controls at call time for any asset they are recognized as owning (including their own freshly created low-value asset). A registered wrapped token with wrong decimals is permanently mis-valued by every wallet, DEX, and integrator on the Ethereum side relative to its real backing asset, which can be leveraged to mislead users/integrators about token value or to create arbitrage opportunities against automated pricing/liquidity systems that trust the ERC20 `decimals()` value. This satisfies the "runtime bugs that compromise intended behavior" and "public underpriced work"/mis-valuation categories in the impact gate, without requiring a malicious relayer, validator, or governance actor.

### Likelihood Explanation
High. `register_token` is a standard, permissionless (asset-owner-gated only) public extrinsic on Asset Hub with no additional validation of the `decimals` field. The existing test suite already exercises exactly this call shape (signed asset owner supplying arbitrary metadata), showing it is a supported and reachable code path, not a hypothetical edge case.

### Recommendation
Before forwarding `metadata` to the backend `RegisterForeignToken` command, validate `metadata.decimals` (and ideally `name`/`symbol`) against the authoritative on-chain metadata of the asset identified by `asset_location` (e.g. via `pallet_assets::Inspect::decimals` for `ForeignAssets`/`Assets`, or the fixed decimals constant for the native `Balances` token), and reject the call with an error if they do not match, rather than trusting caller-supplied values.

### Proof of Concept
1. An account that owns/administers some low-value local asset (e.g. a freshly `pallet_assets::create`d asset with real `decimals = 12`) calls `EthereumSystemFrontend::register_token` on Asset Hub, passing `AssetMetadata { name, symbol, decimals: 0 }`.
2. `RegisterTokenOrigin::ensure_origin` succeeds because the caller does own the asset location; no check on `decimals` is performed (`bridges/snowbridge/pallets/system-frontend/src/lib.rs:225-252`).
3. The XCM `Transact` reaches Bridge Hub's `snowbridge-pallet-system-v2::register_token`, which sends `Command::RegisterForeignToken { decimals: 0, .. }` unchanged (`bridges/snowbridge/pallets/system-v2/src/lib.rs:233-241`).
4. The Ethereum Gateway deploys an immutable ERC20 with `decimals() == 0`, while the real backing asset uses 12 decimals — every subsequent bridged transfer of raw amount `X` will display as `X` whole tokens on Ethereum instead of `X / 10^12`, a permanent 10^12 valuation distortion.

### Citations

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L210-252)
```rust
		/// Initiates the registration for a Polkadot-native token as a wrapped ERC20 token on
		/// Ethereum.
		/// - `asset_id`: Location of the asset
		/// - `metadata`: Metadata to include in the instantiated ERC20 contract on Ethereum
		///
		/// All origins are allowed, however `asset_id` must be a location nested within the origin
		/// consensus system.
		#[pallet::call_index(1)]
		#[pallet::weight(
			T::WeightInfo::register_token()
				.saturating_add(T::BackendWeightInfo::transact_register_token())
				.saturating_add(T::BackendWeightInfo::do_process_message())
				.saturating_add(T::BackendWeightInfo::commit_single())
				.saturating_add(T::BackendWeightInfo::submit_delivery_receipt())
		)]
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

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L319-338)
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

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound.rs (L158-187)
```rust
#[test]
pub fn register_usdt_from_owner_on_asset_hub() {
	fund_on_bh();
	register_assets_on_ah();
	fund_on_ah();
	set_up_eth_and_dot_pool();
	AssetHubWestend::execute_with(|| {
		type RuntimeOrigin = <AssetHubWestend as Chain>::RuntimeOrigin;
		type RuntimeEvent = <AssetHubWestend as Chain>::RuntimeEvent;

		let fees_asset =
			Asset { id: AssetId(Location::parent()), fun: Fungible(1_000_000_000u128) };

		assert_ok!(
			<AssetHubWestend as AssetHubWestendPallet>::SnowbridgeSystemFrontend::register_token(
				RuntimeOrigin::signed(AssetHubWestendAssetOwner::get()),
				bx!(VersionedLocation::from(usdt_at_ah_westend())),
				AssetMetadata {
					name: "usdt".as_bytes().to_vec().try_into().unwrap(),
					symbol: "usdt".as_bytes().to_vec().try_into().unwrap(),
					decimals: 6,
				},
				fees_asset
			)
		);
		assert_expected_events!(
			AssetHubWestend,
			vec![RuntimeEvent::AssetConversion(pallet_asset_conversion::Event::SwapExecuted { .. }) => {},]
		);
	});
```

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound_edge_case.rs (L180-206)
```rust
#[test]
pub fn register_usdt_not_from_owner_on_asset_hub_will_fail() {
	fund_on_bh();
	register_assets_on_ah();
	fund_on_ah();
	AssetHubWestend::execute_with(|| {
		type RuntimeOrigin = <AssetHubWestend as Chain>::RuntimeOrigin;

		let fees_asset =
			Asset { id: AssetId(ethereum()), fun: Fungible(REMOTE_FEE_AMOUNT_IN_ETHER) };

		assert_noop!(
			<AssetHubWestend as AssetHubWestendPallet>::SnowbridgeSystemFrontend::register_token(
				// The owner is Alice, while AssetHubWestendReceiver is Bob, so it should fail
				RuntimeOrigin::signed(AssetHubWestendReceiver::get()),
				bx!(VersionedLocation::from(usdt_at_ah_westend())),
				AssetMetadata {
					name: "usdt".as_bytes().to_vec().try_into().unwrap(),
					symbol: "usdt".as_bytes().to_vec().try_into().unwrap(),
					decimals: 6,
				},
				fees_asset
			),
			BadOrigin
		);
	});
}
```
