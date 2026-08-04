### Title
Permissionless, Unvalidated & Permanent Foreign-Token Metadata Registration Enables Ethereum-Side Token Impersonation - (File: `bridges/snowbridge/pallets/system-v2/src/lib.rs`)

### Summary
The Snowbridge V2 token-registration flow (`snowbridge-pallet-system-frontend::register_token` → `snowbridge-pallet-system-v2::register_token`) lets any account that merely owns/controls a Polkadot-side asset `Location` register a wrapped ERC20 representation of that asset on Ethereum with **arbitrary, unvalidated `name`/`symbol`/`decimals` metadata**. The resulting `token_id → location` mapping is written **once and never overwritten or removable**. This directly mirrors the external report's "malicious complementary contract, once registered, can never be modified" pattern: an attacker who owns some (potentially near-worthless) Polkadot asset location can register it under Ethereum-recognizable branding (e.g. name `"USD Coin"`, symbol `"USDC"`), deceiving Ethereum-side users/wallets/integrators into treating the wrapped token as legitimate, and this cannot ever be corrected afterward.

### Finding Description
`register_token` in `bridges/snowbridge/pallets/system-frontend/src/lib.rs` is callable by "All origins," gated only by `T::RegisterTokenOrigin::ensure_origin(origin, &asset_location)`, which (per `ForeignAssetOwner`/`LocalAssetOwner` in `bridges/snowbridge/runtime/runtime-common/src/v2/register_token.rs`) merely checks that the caller is the *owner* of the asset at `asset_location` — not that the supplied `metadata` (arbitrary `AssetMetadata { name, symbol, decimals }`) is accurate, unique, or non-impersonating: [1](#0-0) [2](#0-1) 

The XCM Transact is forwarded to BridgeHub's `snowbridge-pallet-system-v2::register_token`, gated only by `T::FrontendOrigin`, which then computes `token_id` from the reanchored `location` and **inserts the mapping only if it doesn't already exist** — a first-write-wins, permanent registration with no update/removal call anywhere in the pallet: [3](#0-2) 

The caller-supplied `metadata.name`/`metadata.symbol`/`metadata.decimals` are passed verbatim into `Command::RegisterForeignToken`, which is ABI-encoded into `RegisterForeignTokenParams` and sent to the Ethereum Gateway to deploy/initialize the wrapped ERC20 contract's display metadata: [4](#0-3) 

Nothing in this path validates that `name`/`symbol` correspond to the real, canonical asset, nor prevents squatting well-known symbols with an attacker-owned asset location. The legacy governance-only `register_token` in `snowbridge-pallet-system` (root-gated) shows the same permanent-insert pattern is by design considered acceptable when gated by root; but the V2 frontend variant exposes the identical permanent-write primitive to unprivileged asset owners: [5](#0-4) 

### Impact Explanation
Because the `ForeignToNativeId`/reverse mapping and the on-chain ERC20 metadata are permanent once set (no update, no pause, no removal extrinsic exists in either `system` or `system-v2` pallets), a malicious but unprivileged actor can:
1. Create/own an arbitrary Polkadot-side asset (e.g. via `pallet-assets::create` for a foreign/local asset they control).
2. Call `register_token` supplying metadata that impersonates a well-known, valuable asset (name/symbol of a real stablecoin or token).
3. Permanently register this deceptive ERC20 wrapper on the Ethereum Gateway, which cannot be corrected, renamed, or paused afterward by governance short of a full chain upgrade to the pallet logic.

This can mislead relayers, wallets, DeFi integrators and end users bridging assets to/from Ethereum into interacting with a wrapped token that displays trusted branding but is backed by a worthless/attacker-controlled asset — a direct token-impersonation/theft-adjacent vector, and the affected `token_id` is permanently "burned" for legitimate future use, matching the external report's core harms (permanent unusable/mis-bound pairing, user fund loss via deception, no pause capability).

### Likelihood Explanation
The path only requires an unprivileged signed/XCM origin that owns any asset location on Polkadot's Asset Hub — trivially achievable by any user creating a new asset via `pallet-assets`. No admin, governance, relayer, or validator collusion is required, satisfying the "no privileged actor" constraint of the analog. The main mitigating factor is that the practical deception depends on external actors (Ethereum-side users/tooling) trusting on-chain metadata without off-chain verification of provenance — the same class of assumption failure the external report calls out (no bytecode/metadata verification, no checklist enforcement on-chain).

### Recommendation
- Add validation/allow-listing for `metadata.name`/`metadata.symbol` in `register_token` (e.g., disallow registration of reserved/well-known symbol strings, or require symbol uniqueness enforcement against a maintained registry) before forwarding to `RegisterForeignToken`.
- Introduce a governance-gated `update_token_metadata` / `pause_token` extrinsic in `snowbridge-pallet-system-v2` so an erroneous or malicious registration can be corrected or halted post-hoc, rather than being permanently baked into `ForeignToNativeId` and the deployed ERC20 contract.
- Consider requiring a bonded/deposit-backed registration (with slashing on proven impersonation) or a delay period before the Ethereum-side contract deployment finalizes, allowing objection/challenge.

### Proof of Concept
1. On Asset Hub, an attacker account creates a new local asset via `pallet_assets::create` and becomes its owner (satisfying `LocalAssetOwner`/`ForeignAssetOwner` checks).
2. Attacker calls `EthereumSystemFrontend::register_token(origin, asset_id_of_owned_asset, AssetMetadata { name: b"USD Coin", symbol: b"USDC", decimals: 6 }, fee_asset)`.
3. `RegisterTokenOrigin::ensure_origin` succeeds because attacker owns `asset_id`; call is forwarded via Transact to BridgeHub.
4. `snowbridge-pallet-system-v2::register_token` computes `token_id` for the attacker's location, finds `ForeignToNativeId` empty for that id, inserts it permanently, and emits `Command::RegisterForeignToken { token_id, name: "USD Coin", symbol: "USDC", decimals: 6 }` to the Ethereum Gateway.
5. The Gateway deploys/initializes an ERC20 contract branded "USD Coin"/"USDC" backed only by the attacker's worthless asset; this registration can never be edited, renamed, or paused via any existing extrinsic in `snowbridge-pallet-system` / `snowbridge-pallet-system-v2`.

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

**File:** bridges/snowbridge/runtime/runtime-common/src/v2/register_token.rs (L21-53)
```rust
impl<
		IsForeign: ContainsPair<L, L>,
		AssetInspect: frame_support::traits::fungibles::roles::Inspect<AccountId>,
		AccountId: Eq + Clone,
		LocationToAccountId: xcm_executor::traits::ConvertLocation<AccountId>,
		RuntimeOrigin: From<XcmOrigin> + OriginTrait + Clone,
		L: From<Location> + Into<Location> + Clone,
	> EnsureOriginWithArg<RuntimeOrigin, L>
	for ForeignAssetOwner<IsForeign, AssetInspect, AccountId, LocationToAccountId, L>
where
	for<'a> &'a RuntimeOrigin::PalletsOrigin: TryInto<&'a XcmOrigin>,
	<AssetInspect as frame_support::traits::fungibles::Inspect<AccountId>>::AssetId: From<Location>,
{
	type Success = L;

	fn try_origin(
		origin: RuntimeOrigin,
		asset_location: &L,
	) -> Result<Self::Success, RuntimeOrigin> {
		let origin_location = EnsureXcm::<Everything, L>::try_origin(origin.clone())?;
		if !IsForeign::contains(asset_location, &origin_location) {
			return Err(origin);
		}
		let asset_location: Location = asset_location.clone().into();
		let owner = AssetInspect::owner(asset_location.into()).ok_or_else(|| origin.clone())?;
		let location: Location = origin_location.clone().into();
		let from =
			LocationToAccountId::convert_location(&location).ok_or_else(|| origin.clone())?;
		if from != owner {
			return Err(origin);
		}
		Ok(location.into())
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

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/message.rs (L68-78)
```rust
		// Payload for RegisterForeignToken
		struct RegisterForeignTokenParams {
			/// @dev The token ID (hash of stable location id of token)
			bytes32 foreignTokenID;
			/// @dev The name of the token
			bytes name;
			/// @dev The symbol of the token
			bytes symbol;
			/// @dev The decimal of the token
			uint8 decimals;
		}
```

**File:** bridges/snowbridge/pallets/system/src/lib.rs (L476-509)
```rust
		pub(crate) fn do_register_token(
			location: &Location,
			metadata: AssetMetadata,
			pays_fee: PaysFee<T>,
		) -> Result<(), DispatchError> {
			let ethereum_location = T::EthereumLocation::get();
			// reanchor to Ethereum context
			let location = location
				.clone()
				.reanchored(&ethereum_location, &T::UniversalLocation::get())
				.map_err(|_| Error::<T>::LocationConversionFailed)?;

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
			Self::send(SECONDARY_GOVERNANCE_CHANNEL, command, pays_fee)?;

			Self::deposit_event(Event::<T>::RegisterToken {
				location: location.clone().into(),
				foreign_token_id: token_id,
			});

			Ok(())
		}
```
