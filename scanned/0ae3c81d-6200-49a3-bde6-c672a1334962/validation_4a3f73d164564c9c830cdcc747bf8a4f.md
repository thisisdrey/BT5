## Analysis Summary

The Linea report's core broken invariant is: **a token-identifier lookup that is not scoped per-owner/per-layer lets an unrelated party bind/rebind the mapping for a token they don't control, causing incorrect bridging state.** The closest verified local analog is in Snowbridge's `register_token` extrinsic in the `snowbridge-pallet-system-v2` pallet, which lets any `FrontendOrigin`-satisfying caller register **arbitrary** attacker-chosen `asset_id`/metadata against the global `ForeignToNativeId` map, with no check that the caller controls/owns the underlying asset at that location, and it always forwards attacker-supplied metadata to Ethereum even when the token is already registered.

### Title
Unauthorized re-registration of already-bridged tokens allows metadata spoofing of existing Snowbridge PNAs - (File: `bridges/snowbridge/pallets/system-v2/src/lib.rs`)

### Summary
`Pallet::register_token` in `snowbridge-pallet-system-v2` only requires `T::FrontendOrigin::ensure_origin` — an XCM-origin check, not an asset-ownership check — before accepting a caller-supplied `asset_id: Location` and `metadata: AssetMetadata`. The `token_id` is deterministically derived from the reanchored `Location`, and `ForeignToNativeId` is a single global map keyed by `token_id`. Any account able to trigger this call through the AssetHub frontend (an unprivileged user, since the frontend is meant to proxy arbitrary user calls) can pass the location of an asset that already has a bridged mapping (e.g. DOT, USDT, or any other parachain's PNA) together with forged `name`/`symbol`/`decimals`, and the pallet will still emit a `RegisterForeignToken` command to the Ethereum Gateway with the attacker's metadata — even though `ForeignToNativeId` is left unchanged.

### Finding Description [1](#0-0) 

```
pub fn register_token(
    origin: OriginFor<T>,
    sender: Box<VersionedLocation>,
    asset_id: Box<VersionedLocation>,
    metadata: AssetMetadata,
    amount: u128,
) -> DispatchResult {
    T::FrontendOrigin::ensure_origin(origin)?;
    ...
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
    ...
    Self::send(message_origin, command, amount)?;
    ...
}
```

There is no check anywhere in this path that:
1. `sender_location`/the calling account is the actual owner, issuer, or creator of the asset at `asset_location`.
2. `token_id` is not already registered before dispatching the `RegisterForeignToken` command with attacker-supplied metadata.

The `if !ForeignToNativeId::<T>::contains_key(...)` guard only prevents the **mapping** from being overwritten — it does not prevent the outbound `Command::RegisterForeignToken` from being sent again with different metadata for a `token_id` that already backs a live, previously-bridged native asset. The Gateway/Command executes on Ethereum keyed purely by `token_id` (see the `RegisterForeignToken` command consumed on the Ethereum side to (re)configure the ERC20 wrapper's name/symbol/decimals), so a second, unrelated, unprivileged registration call rewrites the on-chain representation (name/symbol/decimals) of somebody else's already-bridged token.

This is structurally the same class of bug as the Linea report: the mapping/registration primitive is not scoped to the entity that legitimately owns the underlying asset on a given layer, so a party with no relationship to the real token can cause the bridge to treat/label it incorrectly — in Linea's case by hijacking the bridging transfer itself, here by hijacking the wrapped token's declared metadata (and, for a not-yet-registered but real asset location, by front-running the legitimate registration with a different `token_id` binding of attacker's choice before the genuine owner ever calls `register_token`).

### Impact Explanation
- Any account that can route a call through `T::FrontendOrigin` (designed to accept AssetHub-forwarded, user-initiated XCM calls, not privileged governance) can spoof the ERC20 `name`/`symbol`/`decimals` of an already-registered, real bridged Polkadot-native token on Ethereum, degrading trust in and usability of the bridge's wrapped assets without needing any admin, relayer, or validator compromise.
- For not-yet-registered valuable asset locations, an attacker can preemptively register the location with their own metadata (and win the `token_id` -> `Location` binding permanently, since the map only accepts the first writer), denying/corrupting future legitimate registration by the real asset issuer — a form of permanent state lock/incorrect binding matching the "duplicate settlement/incorrect beneficiary" impact class.
- This directly touches Snowbridge BridgeHub public-dispatch code in scope for the HackenProof program.

### Likelihood Explanation
High: `register_token` is a public, non-privileged extrinsic (only gated by an `EnsureOrigin` implementation meant to authenticate XCM callers generically, not asset ownership), takes fully attacker-controlled `asset_id` and `metadata` parameters, and the vulnerable code path (`Command::RegisterForeignToken` dispatch regardless of `contains_key` result) is unconditionally reached on every call.

### Recommendation
- Require an ownership/authorization check binding `sender_location` (or the AssetHub asset's registered owner/admin) to the `asset_id` being registered, analogous to how `pallet-assets` verifies the asset owner before privileged operations.
- Make registration strictly idempotent/one-shot: if `token_id` already exists in `ForeignToNativeId`, reject the call (return an error) instead of silently skipping the map update while still sending the `RegisterForeignToken` command.
- Consider scoping registration state per-origin/per-layer as the Linea fix did, so that a `token_id`/metadata binding can only ever be set once by the legitimate owning entity.

### Proof of Concept
1. Party A legitimately registers PNA location `L` (e.g., DOT: `Location::parent()`), producing `token_id = TokenIdOf::convert_location(L)`; `ForeignToNativeId[token_id] = L` is set and Ethereum's Gateway registers ERC20 metadata `("Polkadot", "DOT", 10)`.
2. Attacker B, with no relationship to DOT, calls `register_token(origin, sender=B_location, asset_id=L, metadata={name:"Fake",symbol:"FAKE",decimals:0}, amount)` through any account able to satisfy `T::FrontendOrigin` (e.g., an AssetHub-forwarded user XCM call, as demonstrated by `Test::FrontendOrigin`/`make_xcm_origin` usage in [2](#0-1)  which shows the frontend accepting arbitrary user locations).
3. `ForeignToNativeId::<T>::contains_key(token_id)` is `true`, so the map is left unchanged — but `Command::RegisterForeignToken { token_id, name: "Fake", symbol: "FAKE", decimals: 0 }` is still built and sent via `Self::send(...)`, with no error returned to B.
4. The Gateway on Ethereum receives the second `RegisterForeignToken` command for the same `token_id` and overwrites the existing ERC20's metadata, corrupting the on-chain representation of DOT with attacker-chosen values, even though B never legitimately controlled `L`.

### Citations

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

**File:** bridges/snowbridge/pallets/system-v2/src/tests.rs (L124-135)
```rust
	for tc in test_cases.iter() {
		new_test_ext(true).execute_with(|| {
			let origin = make_xcm_origin(FrontendLocation::get());
			let versioned_location: VersionedLocation = tc.native.clone().into();

			assert_ok!(EthereumSystemV2::register_token(
				origin,
				Box::new(versioned_location.clone()),
				Box::new(versioned_location),
				Default::default(),
				1
			));
```
