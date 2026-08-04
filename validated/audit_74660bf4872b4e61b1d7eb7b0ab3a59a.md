### Title
`register_token` in `pallet-snowbridge-system-v2` accepts an unverified `sender` location instead of the actual XCM origin - (File: `bridges/snowbridge/pallets/system-v2/src/lib.rs`)

### Summary
The external report flags `addLender` for trusting a caller-supplied value (`want`) without checking it against the value that should actually govern the operation (the Lender's own `want`). The local analog is `Pallet::register_token` in the Snowbridge System V2 pallet: it authenticates the call with `T::FrontendOrigin::ensure_origin(origin)`, which resolves to a verified `Location` (`Success = Location`), but then **discards that verified location** and instead trusts a caller-supplied `sender: Box<VersionedLocation>` parameter to compute the message origin used for agent/fee attribution on Ethereum. [1](#0-0) 

### Finding Description
`register_token` is called via `T::FrontendOrigin`, an XCM-origin filter (configured in `bridge_to_ethereum_config.rs`) that is meant to restrict which chains/locations (e.g. Asset Hub or approved frontends) may proxy this call to BridgeHub. Its `ensure_origin` call returns the actual, verified `Location` of the caller as its `Success` type: [2](#0-1) 

However, the return value is discarded (`T::FrontendOrigin::ensure_origin(origin)?;` with no binding). Instead, the function uses the caller-supplied `sender` parameter — fully attacker/XCM-payload controlled — to derive `sender_location`, which then feeds `Self::location_to_message_origin(sender_location)` and becomes the `origin: H256` field of the outbound `Message` sent to the Gateway: [3](#0-2) 

`location_to_message_origin` reanchors the location and converts it into an agent ID hash (`LocationHashOf::convert_location`), which on the Ethereum side determines which agent contract's balance is charged execution/delivery fees, and which entity is treated as the command's origin: [4](#0-3) 

Because the verified origin from `FrontendOrigin` is never compared against `sender`, any location permitted to call through `FrontendOrigin` (e.g. any account that can route an XCM `Transact` from Asset Hub with `preserve_origin`/`AliasOrigin`, as seen in the `EthereumSystemFrontend::RegisterToken` test flows) can supply an arbitrary `sender` location — including one belonging to another parachain, another user, or a privileged agent — to redirect fee attribution and the message-origin agent binding to that arbitrary identity instead of its own. [5](#0-4) 

This is structurally identical to the Yearn bug: the function accepts and trusts an externally supplied identity/value (`sender` ~ `want`) without checking it equals the value that authentication already established (`FrontendOrigin`'s verified `Location` ~ Lender's `want`).

### Impact Explanation
An unauthorized caller able to invoke `register_token` through any permitted `FrontendOrigin` path can misattribute the outbound message's origin/agent to a different, unrelated location. Depending on how agents pay for outbound queue delivery/execution fees on the Ethereum side, this allows fee costs to be charged against another party's agent balance, or a command to be recorded as though it were initiated by a different origin than the real caller — an origin-escalation / misattribution primitive that violates the "message queues ... must bind ... origin ... exactly once" invariant central to the Polkadot SDK impact gate.

### Likelihood Explanation
Likelihood is moderate to high: the vulnerable code path is directly reachable by any signed/XCM origin accepted by `FrontendOrigin` (this is explicitly designed to be called cross-chain, e.g. from Asset Hub, as shown in the `EthereumSystemFrontend::RegisterToken` integration tests), and no additional privilege, governance, or malicious-relayer assumption is required — the caller simply supplies a `sender` value of their choosing.

### Recommendation
Bind and use the `Location` returned by `T::FrontendOrigin::ensure_origin(origin)?` directly for computing `message_origin` (or explicitly assert `sender_location == verified_origin`) instead of trusting the caller-supplied `sender` parameter. If `sender` is meant to represent a sub-identity of the verified origin (e.g., a specific account under an Asset Hub sovereign), constrain it so it must be a descendant of the verified `Location`, analogous to checking `want` equals the Lender's `want` in the original report.

### Proof of Concept
1. An account able to satisfy `T::FrontendOrigin` (e.g., an XCM `Transact` from any chain permitted by the frontend origin filter) calls `register_token(origin, sender, asset_id, metadata, amount)`.
2. Set `sender` to an arbitrary `VersionedLocation` (e.g., Asset Hub's own sovereign location, or another parachain's location) rather than the caller's true location.
3. `T::FrontendOrigin::ensure_origin(origin)` succeeds and returns the real verified `Location`, but this value is discarded at line 218.
4. `sender_location` (attacker-controlled) is reanchored and hashed via `location_to_message_origin`, producing an `H256` agent id that does not correspond to the real caller.
5. `Self::send(message_origin, command, amount)` dispatches the `RegisterForeignToken` command attributing origin/fee responsibility to the spoofed agent rather than the actual caller, as observed in the existing test at [6](#0-5) 
which itself passes a `sender`/origin path from a caller-controlled XCM program without further validation against the FrontendOrigin's resolved `Location`.

### Citations

**File:** bridges/snowbridge/pallets/system-v2/src/lib.rs (L211-241)
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
```

**File:** bridges/snowbridge/pallets/system-v2/src/lib.rs (L302-313)
```rust
		/// Reanchor the `location` in context of ethereum
		pub fn reanchor(location: Location) -> Result<Location, Error<T>> {
			location
				.reanchored(&T::EthereumLocation::get(), &T::UniversalLocation::get())
				.map_err(|_| Error::<T>::LocationReanchorFailed)
		}

		pub fn location_to_message_origin(location: Location) -> Result<H256, Error<T>> {
			let reanchored_location = Self::reanchor(location)?;
			LocationHashOf::convert_location(&reanchored_location)
				.ok_or(Error::<T>::LocationConversionFailed)
		}
```

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound.rs (L125-146)
```rust
#[test]
pub fn register_relay_token_from_asset_hub_with_sudo() {
	fund_on_bh();
	register_assets_on_ah();
	fund_on_ah();
	AssetHubWestend::execute_with(|| {
		type RuntimeOrigin = <AssetHubWestend as Chain>::RuntimeOrigin;

		let fees_asset = Asset { id: AssetId(ethereum()), fun: Fungible(1) };

		assert_ok!(
			<AssetHubWestend as AssetHubWestendPallet>::SnowbridgeSystemFrontend::register_token(
				RuntimeOrigin::root(),
				bx!(VersionedLocation::from(Location { parents: 1, interior: [].into() })),
				AssetMetadata {
					name: "wnd".as_bytes().to_vec().try_into().unwrap(),
					symbol: "wnd".as_bytes().to_vec().try_into().unwrap(),
					decimals: 12,
				},
				fees_asset
			)
		);
```

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound.rs (L730-738)
```rust
			Asset { id: AssetId(ethereum()), fun: Fungible(REMOTE_FEE_AMOUNT_IN_ETHER) };

		let call = EthereumSystemFrontend::EthereumSystemFrontend(
			EthereumSystemFrontendCall::RegisterToken {
				asset_id: Box::new(VersionedLocation::from(foreign_asset_at_asset_hub)),
				metadata: Default::default(),
				fee_asset: remote_fee_asset_on_ethereum.clone(),
			},
		);
```
