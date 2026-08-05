Audit Report

## Title
`register_token` allows an already-registered token's owner to re-register and overwrite its Ethereum-side metadata (name/symbol/decimals) via an unconditional `Command::RegisterForeignToken` - ([File: bridges/snowbridge/pallets/system/src/lib.rs])

## Summary
`do_register_token` in `bridges/snowbridge/pallets/system/src/lib.rs` and the duplicated logic in `bridges/snowbridge/pallets/system-v2/src/lib.rs::register_token` guard only the `ForeignToNativeId` storage insert with `if !ForeignToNativeId::<T>::contains_key(token_id)`, but always build and dispatch a fresh `Command::RegisterForeignToken` carrying the caller-supplied `metadata.name`/`symbol`/`decimals`, regardless of whether `token_id` is already registered. Because `token_id` is deterministically derived from the reanchored asset `location` via `TokenIdOf::convert_location`, calling the registration path twice for the same asset produces the same `token_id` but can carry different metadata, letting the token's owner overwrite the ERC20 metadata (in particular `decimals`) of an already-live bridged token on Ethereum.

## Finding Description
`do_register_token`: [1](#0-0) , and the `system-v2` `register_token`: [2](#0-1)  both share the identical flaw: the `contains_key` check controls only the `ForeignToNativeId::insert` call, not the construction/dispatch of `Command::RegisterForeignToken`.

The entry point reachable by a non-root, non-governance actor is `system-frontend`'s `register_token`, gated by `T::RegisterTokenOrigin::ensure_origin`: [3](#0-2) . Unlike the claim's characterization of "any origin," the actual origin implementation, `ForeignAssetOwner` in `bridges/snowbridge/runtime/runtime-common/src/v2/register_token.rs`, requires the caller to be the on-chain *owner* of the target foreign asset: [4](#0-3) . This is confirmed by the emulated test `register_usdt_not_from_owner_on_asset_hub_will_fail`, which shows a non-owner signed origin is rejected with `BadOrigin`: [5](#0-4) .

So the exploitable actor is not "any origin" but specifically the asset's registered owner. That owner is legitimately allowed to call `register_token` once; there is no check anywhere in `do_register_token` (or its `system-v2` counterpart) preventing that same owner from calling it a second time with different `metadata.decimals`/`name`/`symbol` for the same `location`/`token_id`. The `contains_key` check is purely an optimization to avoid redundant storage writes — it never becomes an `ensure!`/rejection, so the outbound `Command::RegisterForeignToken` (which the Gateway contract on Ethereum uses to (re)configure ERC20 metadata) is sent unconditionally every time, carrying whatever metadata the caller supplies in that particular call.

## Impact Explanation
Because the on-chain guard is inconsistent — blocking the redundant storage write but not the outbound governance-channel/frontend-channel command — the asset owner can cause the Gateway contract to receive a metadata-changing `RegisterForeignToken` command for a `token_id` that already has bridged value outstanding under the original metadata. A `decimals` change in particular breaks the fixed conversion factor between the Polkadot-native asset and its Ethereum wrapped representation for an already-active bridged token, which can cause a mismatch between minted/locked amounts and their represented value, or cause the bridge/Gateway to reject or mishandle metadata conflicts for a live token, stalling bridge processing. This aligns with the "runtime bugs that compromise intended behavior" and "public underpriced work that degrades block production or stalls bridge processing" categories in the impact gate, since the state corrupted is the token's declared `decimals`/`name`/`symbol` sent in the `RegisterForeignToken` command relative to the already-registered `token_id`/`location` mapping in `ForeignToNativeId`.

## Likelihood Explanation
The attacker must be the legitimate owner of a foreign asset that has already been registered once via `register_token` — this is not a fully permissionless "any origin" exploit as originally framed, since `ForeignAssetOwner`/equivalent origin checks require asset ownership. However, once an asset is registered, its owner retains the ability to call `register_token` again at will with different metadata, and no `AlreadyRegistered`-style guard exists in `do_register_token` or in the `system-v2` `register_token` dispatchable to prevent this. This makes the issue readily and repeatably triggerable by the asset owner without requiring any additional privilege escalation, governance action, or compromised infrastructure.

## Recommendation
In `do_register_token` (`bridges/snowbridge/pallets/system/src/lib.rs`) and in `system-v2::register_token` (`bridges/snowbridge/pallets/system-v2/src/lib.rs`), add an explicit rejection (e.g., `ensure!(!ForeignToNativeId::<T>::contains_key(token_id), Error::<T>::TokenAlreadyRegistered)`) before constructing and sending `Command::RegisterForeignToken`, rather than only using that check to skip the storage insert. If metadata updates for already-registered tokens are an intended feature, they should be split into a separate, explicitly-named and appropriately-gated dispatchable (e.g., `update_token_metadata`) rather than silently overloading `register_token`.

## Proof of Concept
1. Asset owner `A` owns asset location `L` on Asset Hub and calls `SnowbridgeSystemFrontend::register_token(asset_id = L, metadata = {name: "Foo", symbol: "FOO", decimals: 18}, fee_asset)`. This is proxied via XCM `transact` to `system-v2::register_token` on BridgeHub, which inserts `ForeignToNativeId[token_id] = L` and sends `Command::RegisterForeignToken{token_id, "Foo", "FOO", 18}` to the Gateway contract, per `bridges/snowbridge/pallets/system-v2/src/lib.rs` lines 229-241.
2. Users bridge value assuming 18 decimals for the wrapped ERC20.
3. Owner `A` calls `register_token` again for the same `asset_id = L` with `metadata = {decimals: 6, ...}`.
4. `ForeignToNativeId::contains_key(token_id)` is `true`, so the insert is skipped, but the pallet still unconditionally builds and sends a second `Command::RegisterForeignToken{token_id, ..., decimals: 6}`, per the same code path — with no guard rejecting this second, metadata-conflicting registration for an already-registered `token_id`.

### Citations

**File:** bridges/snowbridge/pallets/system/src/lib.rs (L488-501)
```rust
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
```

**File:** bridges/snowbridge/pallets/system-v2/src/lib.rs (L225-241)
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

			let message_origin = Self::location_to_message_origin(sender_location)?;
			Self::send(message_origin, command, amount)?;
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
