Audit Report

## Title
Unvalidated `AssetMetadata` name/symbol in `register_token` allows any asset owner to mint an Ethereum ERC20 wrapper impersonating a legitimate token's ticker - (File: `bridges/snowbridge/pallets/system-frontend/src/lib.rs`, `bridges/snowbridge/pallets/system-v2/src/lib.rs`)

## Summary
`SnowbridgeSystemFrontend::register_token` and the downstream `EthereumSystem::register_token` authorize the caller only against the `asset_id` `Location` (via `RegisterTokenOrigin`, which in production is `LocalAssetOwner`/`ForeignAssetOwner` requiring ownership of the underlying `pallet_assets`/`pallet_assets` foreign asset), but never validate the caller-supplied `AssetMetadata.name`/`symbol`/`decimals` against the asset's actual on-chain metadata. This lets any account that creates and owns a (permissionlessly creatable) asset register a wrapped ERC20 on Ethereum carrying an arbitrary, attacker-chosen ticker/name (e.g. "USDC"), even though the backing asset is worthless and unrelated to any real token of that name.

## Finding Description
On AssetHub, `register_token` checks only that the origin owns `asset_location`: [1](#0-0) 
and passes `metadata` through untouched into the XCM `Transact` call built by `build_register_token_call`: [2](#0-1) 

The production configuration of `RegisterTokenOrigin` on AssetHub Westend confirms this is strictly an ownership check on the `Location`, with no metadata validation: [3](#0-2) 

The underlying `LocalAssetOwner`/`ForeignAssetOwner` origin implementations in `bridges/snowbridge/runtime/runtime-common/src/v2/register_token.rs` verify only that the signer/XCM-origin is the registered `owner` of the asset at that `Location` (via `AssetInspect::owner`) — they never read or compare against `pallet_assets::Metadata` (the asset's real name/symbol/decimals): [4](#0-3) [5](#0-4) 

On BridgeHub, `snowbridge_pallet_system_v2::register_token` again forwards `metadata.name`/`symbol`/`decimals` verbatim into `Command::RegisterForeignToken`, keyed only by `token_id = TokenIdOf::convert_location(&location)`: [6](#0-5) 

Since `pallet_assets`/foreign-assets creation is permissionless (subject only to a deposit), an attacker can create a worthless asset they own, then legitimately pass the `RegisterTokenOrigin` ownership check for that `Location`, and supply arbitrary `metadata.name`/`symbol` (e.g. "USD Coin"/"USDC") that has no relationship to the actual registered metadata of any real USDC asset. The resulting `Command::RegisterForeignToken` is dispatched to the Ethereum Gateway with these free-form `name`/`symbol` fields: [7](#0-6) 

No code path in `register_token` (frontend, system-v2, or legacy `system`) cross-checks `metadata` against `pallet_assets::Metadata` for the resolved asset, so the ERC20's displayed identity is fully decoupled from the backing asset's real, canonical metadata.

## Impact Explanation
The mis-bound state is the pair `(token_id/Location, name/symbol)` sent to Ethereum in `Command::RegisterForeignToken`: the `token_id` correctly and uniquely identifies the backing Polkadot asset, but `name`/`symbol` are attacker-controlled strings with no cryptographic or on-chain binding to the real asset they purport to represent. This matches the "forged or mis-bound...state acceptance" impact category — the protocol itself, not a malicious relayer or validator, produces state that misrepresents a worthless asset as a well-known token by ticker, which can deceive wallets, bridge front-ends, and integrators that key off symbol/name rather than the opaque `token_id`, leading to fund loss or diverted liquidity/trust decisions.

## Likelihood Explanation
The attack path requires only two unprivileged, single-transaction actions: (1) create an asset via the standard, permissionless `pallet_assets`/foreign-assets creation extrinsic, and (2) call the public `register_token` extrinsic supplying arbitrary metadata for that owned asset. Both steps are reachable by any account with sufficient balance to pay the asset creation deposit, require no governance, relayer collusion, or validator control, and are fully repeatable for any desired ticker string.

## Recommendation
Derive `name`/`symbol`/`decimals` for the `RegisterForeignToken` command from the canonical on-chain `pallet_assets::Metadata` already stored for the resolved `AssetId`/`Location`, rather than accepting an arbitrary caller-supplied `AssetMetadata` structure in `register_token`. If a caller-supplied override is required, enforce global uniqueness of `symbol`/`name` per Gateway and/or require that they exactly match the asset's registered `pallet_assets::Metadata` before forwarding to `Command::RegisterForeignToken`.

## Proof of Concept
1. On AssetHub, call `pallet_assets::create` (or the ForeignAssets equivalent) to create asset `X` at `Location L_attacker`, owned by the attacker (permissionless subject to deposit).
2. Call `SnowbridgeSystemFrontend::register_token(origin=attacker, asset_id=L_attacker, metadata={name:"USD Coin", symbol:"USDC", decimals:6}, fee_asset=...)`.
3. `T::RegisterTokenOrigin::ensure_origin` (`LocalAssetOwner`/`ForeignAssetOwner`) succeeds because the attacker owns `L_attacker`; `metadata` is never compared to `L_attacker`'s real registered metadata.
4. The XCM `Transact` reaches BridgeHub's `EthereumSystem::register_token`, emitting `Command::RegisterForeignToken{ token_id: TokenIdOf(L_attacker), name: "USD Coin", symbol: "USDC", decimals: 6 }`.
5. The Ethereum Gateway deploys/labels a wrapped ERC20 displaying "USDC" that is in fact backed by the attacker's worthless asset `X`, deceiving any party that identifies bridged tokens by name/symbol instead of `token_id`.

### Citations

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L233-236)
```rust
			let asset_location: Location =
				(*asset_id).try_into().map_err(|_| Error::<T>::UnsupportedLocationVersion)?;
			let origin_location = T::RegisterTokenOrigin::ensure_origin(origin, &asset_location)?;

```

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L320-337)
```rust
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
```

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/src/bridge_to_ethereum_config.rs (L50-71)
```rust
	type RegisterTokenOrigin = EitherOf<
		EitherOf<
			LocalAssetOwner<
				AssetIdForTrustBackedAssetsConvert<TrustBackedAssetsPalletLocation, Location>,
				Assets,
				AccountId,
				AssetIdForTrustBackedAssets,
				Location,
			>,
			ForeignAssetOwner<
				(
					FromSiblingParachain<parachain_info::Pallet<Runtime>, Location>,
					xcm_config::bridging::to_rococo::RococoAssetFromAssetHubRococo,
				),
				ForeignAssets,
				AccountId,
				LocationToAccountId,
				Location,
			>,
		>,
		EnsureRootWithSuccess<AccountId, RootLocation>,
	>;
```

**File:** bridges/snowbridge/runtime/runtime-common/src/v2/register_token.rs (L36-53)
```rust
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

**File:** bridges/snowbridge/runtime/runtime-common/src/v2/register_token.rs (L85-96)
```rust
	fn try_origin(
		origin: RuntimeOrigin,
		asset_location: &L,
	) -> Result<Self::Success, RuntimeOrigin> {
		let who = ensure_signed(origin.clone()).map_err(|_| origin.clone())?;
		let asset_id = MatchAssetId::convert(asset_location).ok_or_else(|| origin.clone())?;
		let owner = AssetInspect::owner(asset_id.into()).ok_or_else(|| origin.clone())?;
		if who != owner {
			return Err(origin);
		}
		Ok(who.into())
	}
```

**File:** bridges/snowbridge/pallets/system-v2/src/lib.rs (L225-238)
```rust
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
```

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/message.rs (L162-172)
```rust
	/// Register foreign token from Polkadot
	RegisterForeignToken {
		/// ID for the token
		token_id: H256,
		/// Name of the token
		name: Vec<u8>,
		/// Short symbol for the token
		symbol: Vec<u8>,
		/// Number of decimal places
		decimals: u8,
	},
```
