Confirmed - no `TokenAlreadyRegistered` error exists anywhere in the codebase, and the code matches the claim exactly in both files.

Audit Report

## Title
Missing duplicate-registration guard in Snowbridge `register_token` allows repeated `RegisterForeignToken` bridge commands for an already-registered asset - (File: `bridges/snowbridge/pallets/system-v2/src/lib.rs`, `bridges/snowbridge/pallets/system/src/lib.rs`)

## Summary
`do_register_token` in `bridges/snowbridge/pallets/system/src/lib.rs` and `register_token` in `bridges/snowbridge/pallets/system-v2/src/lib.rs` only guard the `ForeignToNativeId` storage insert with `if !ForeignToNativeId::<T>::contains_key(token_id)`, but unconditionally proceed to build and dispatch `Command::RegisterForeignToken` and emit `Event::RegisterToken` regardless of prior registration. This allows any account satisfying `ForeignAssetOwner`/`RegisterTokenOrigin` to repeatedly re-send `RegisterForeignToken` commands with attacker-chosen `name`/`symbol`/`decimals` metadata for an asset that is already registered.

## Finding Description
In `bridges/snowbridge/pallets/system/src/lib.rs`, `do_register_token` computes `token_id` and checks `ForeignToNativeId::<T>::contains_key(token_id)` only to decide whether to skip the storage insert; there is no `ensure!`/early return when the token is already registered [1](#0-0) . Execution always falls through to `Self::send(SECONDARY_GOVERNANCE_CHANNEL, command, pays_fee)` and `Self::deposit_event(Event::<T>::RegisterToken {...})`.

The identical pattern exists in the v2, user-facing path in `bridges/snowbridge/pallets/system-v2/src/lib.rs`'s `register_token` extrinsic, gated only by `T::FrontendOrigin::ensure_origin(origin)` [2](#0-1) . This is reachable from an unprivileged user via `snowbridge-pallet-system-frontend`'s `register_token` extrinsic, gated by `RegisterTokenOrigin: EnsureOriginWithArg<..., Location>`, implemented by `ForeignAssetOwner`, which only checks that the caller is the owner of the asset location being registered [3](#0-2) . There is no check anywhere in this call chain that rejects registration when `token_id` is already present in `ForeignToNativeId`; the `contains_key` branch is confirmed to gate only the storage write, not the command dispatch, in the actual repository source. No `TokenAlreadyRegistered`-style error variant exists in the codebase.

## Impact Explanation
This violates the intended one-time-registration invariant for `RegisterForeignToken`: the pallet's storage bookkeeping is idempotent, but the outbound `Command::RegisterForeignToken` message stream to the Ethereum Gateway is not deduplicated, and each resend carries independently attacker-controlled `name`/`symbol`/`decimals` metadata for the same `token_id`. This corrupts the exact value of the registration metadata delivered on-chain (Ethereum-side) for a token, allowing the token's displayed name/symbol/decimals to be repeatedly overwritten after the token's initial, presumably governance-sanctioned, registration — a duplicate/inconsistent settlement of registration state across the bridge boundary.

## Likelihood Explanation
High reachability: any account that is the owner of an already-registered foreign asset (satisfying `ForeignAssetOwner`) can call `register_token` on `system-frontend` repeatedly, paying only the normal fee-asset swap/burn cost each time — no governance, root, or privileged access is required. The only friction is the fee, which does not prevent repetition, only limits volume.

## Recommendation
Add an explicit `ensure!(!ForeignToNativeId::<T>::contains_key(token_id), Error::<T>::TokenAlreadyRegistered)` guard before the storage insert in both `do_register_token` (`bridges/snowbridge/pallets/system/src/lib.rs`) and `register_token` (`bridges/snowbridge/pallets/system-v2/src/lib.rs`), so the `Command::RegisterForeignToken` dispatch and `RegisterToken` event are only produced on first registration, and add a corresponding `TokenAlreadyRegistered` error variant to each pallet's `Error` enum.

## Proof of Concept
1. Asset owner `A` calls `register_token(asset_id = X, metadata = M1, fee_asset = F)` via `system-frontend`. `ForeignToNativeId[token_id(X)]` is set, `Command::RegisterForeignToken{token_id, name: M1.name, ...}` is sent to Ethereum, fee `F` is burned.
2. `A` calls `register_token(asset_id = X, metadata = M2, fee_asset = F)` again. `contains_key(token_id)` is `true`, so the insert is skipped, but `Self::send(...)` still dispatches `Command::RegisterForeignToken{token_id, name: M2.name, ...}` and `Event::RegisterToken` is emitted again, with no error raised — confirmed by tracing the code path directly in `bridges/snowbridge/pallets/system-v2/src/lib.rs` lines 229-249 and `bridges/snowbridge/pallets/system/src/lib.rs` lines 491-509.
3. Repeating step 2 lets `A` emit unbounded `RegisterForeignToken` commands with arbitrarily different metadata for the same `token_id`, which a Rust unit test asserting `System::events()` count and command payload divergence across two calls to `register_token` with the same `asset_id` but different `metadata` would confirm.

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

**File:** bridges/snowbridge/runtime/runtime-common/src/v2/register_token.rs (L13-53)
```rust
/// Origin check that verifies that an origin is the owner of a foreign asset.
/// 1. Allows XCM origins
/// 2. Checks that the asset exists
/// 3. The origin must be the owner of the asset
pub struct ForeignAssetOwner<IsForeign, AssetInspect, AccountId, LocationToAccountId, L = Location>(
	core::marker::PhantomData<(IsForeign, AssetInspect, AccountId, LocationToAccountId, L)>,
);

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
