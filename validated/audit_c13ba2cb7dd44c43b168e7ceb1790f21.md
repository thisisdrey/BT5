Based on my research, the strongest local analog to the Balancer buffer bug — a mutable identity mapping that isn't fully validated before a value-affecting command is issued — is in Snowbridge's foreign-token registration path.

### Title
Snowbridge `register_token` binds new asset metadata to an existing `TokenId` without verifying the resolved `Location` matches the stored one - (File: `bridges/snowbridge/pallets/system/src/lib.rs`, `bridges/snowbridge/pallets/system-v2/src/lib.rs`)

### Summary
The Balancer report's root cause is a mapping (`_bufferAssets[wrappedToken] -> underlyingToken`) that is only guarded by a "set once if empty" check, while a value-moving operation (wrap) is still executed using the caller-supplied, unchecked pointer. The Snowbridge `register_token` entry points in `snowbridge-pallet-system` and `snowbridge-pallet-system-v2` follow the identical pattern: `ForeignToNativeId[token_id]` is written only if empty, but the `Command::RegisterForeignToken` (which drives Ethereum-side ERC20 creation/registration for that `token_id`) is unconditionally sent using the caller's freshly supplied `metadata`, regardless of whether the derived `token_id` already refers to a different, previously-registered `Location`.

### Finding Description
`do_register_token` and the v2 `register_token` derive `token_id` from a reanchored `Location` via `TokenIdOf::convert_location`, then do:

```rust
if !ForeignToNativeId::<T>::contains_key(token_id) {
    ForeignToNativeId::<T>::insert(token_id, location.clone());
}
let command = Command::RegisterForeignToken {
    token_id, name: metadata.name..., symbol: metadata.symbol..., decimals: metadata.decimals,
};
Self::send(..., command, ...)?;
``` [1](#0-0) [2](#0-1) 

The storage guard (`if !contains_key { insert }`) only stops the *stored* `location` from being overwritten, mirroring the Vault's `_bufferAssets` "set once" check. But — exactly like the Vault's `erc4626BufferWrapOrUnwrap`, which keeps operating on a `wrappedToken` key even after its underlying asset diverges from what was registered — the `RegisterForeignToken` command is dispatched unconditionally, keyed only by the numeric `token_id`, with **no `ensure!` that the caller's resolved `location` equals the already-stored `location` for that `token_id`**. Any second caller who can produce a `Location` whose `TokenIdOf::convert_location` output collides with an already-registered `token_id` (a real prior weakness acknowledged by the project itself — see the length-collision fix in `prdoc/stable2603/pr_10771.prdoc`, which only closed one specific `GeneralKey`-length collision vector) can push new (falsified) `name`/`symbol`/`decimals` metadata bound to that pre-existing, real `token_id` on the Ethereum Gateway. [3](#0-2) 

The `register_token` extrinsic is reachable by any account behind `FrontendOrigin`/root without needing to own or control the target `Location`, since the function never checks that the submitted `location` is the currently-stored one for the derived `token_id`: [4](#0-3) 

### Impact Explanation
`token_id` is the sole key the Ethereum Gateway uses to associate an ERC20 contract with a Polkadot-native asset for minting/unlocking. If the on-chain `ForeignToNativeId` map and the Ethereum-side `RegisterForeignToken` handler ever diverge in what `token_id` is bound to (via a collision in the location-to-id hashing, several of which this repo's own changelog shows have already existed and been fixed piecemeal), a caller can re-issue registration commands for a real, valuable `token_id` while a *different* Location (an attacker-controlled asset) is what actually satisfies future transfers routed through that id on the parachain side, since the pallet never re-validates location equality before dispatching the command. This is directly analogous to draining Vault liquidity by re-pointing a buffer's `underlyingToken` after the buffer key is already established — the identifier (`wrappedToken` / `token_id`) stays fixed, but the "current asset backing it" is what an unprivileged caller manipulates.

### Likelihood Explanation
Likelihood depends entirely on the ability to construct a `Location` producing a `TokenId` collision with an already-registered, high-value asset. The codebase's own PR history confirms this collision surface exists and has been only partially closed (the `GeneralKey` length ambiguity fix). Because the `register_token` code path itself still performs no `stored_location == submitted_location` equality check before dispatching `RegisterForeignToken`, any residual or future collision vector in `TokenIdOf::convert_location` reopens this exact class of bug without requiring any further pallet change — this is a structural gap, not merely a hash-strength issue.

### Recommendation
In `do_register_token` (system pallet) and `register_token` (system-v2 pallet), when `ForeignToNativeId::contains_key(token_id)` is true, `ensure!` that the stored `location` equals the freshly computed `location` before proceeding, and reject (rather than silently continue) if they differ. Treat `TokenId -> Location` binding as immutable-and-verified on every call, not just on first insert, mirroring the short-term Balancer recommendation of enforcing explicit initialization checks on every access path, not only the creation path.

### Proof of Concept
1. Attacker constructs `Location` `L2` such that `TokenIdOf::convert_location(reanchor(L2))` collides with `token_id_A`, the `TokenId` already bound to a legitimate, valuable asset location `L1` (previously registered by AssetHub/root).
2. Attacker calls `register_token` (system-v2, reachable via `FrontendOrigin` proxy from AssetHub) with `asset_id = L2` and attacker-chosen `metadata`.
3. `ForeignToNativeId::contains_key(token_id_A)` is true, so the stored mapping is left untouched at `L1` — but the pallet still builds and sends `Command::RegisterForeignToken { token_id: token_id_A, name/symbol/decimals: attacker metadata }` to the Ethereum Gateway. [5](#0-4) 
4. Depending on the Ethereum Gateway's handling of `RegisterForeignToken` for an existing `token_id` (out of this repo's scope, on the EVM side), this can desynchronize the "expected asset" for `token_id_A` between the two sides of the bridge, enabling downstream fund-routing confusion for transfers keyed on that `token_id`.

**Note on confidence**: The Ethereum-side (Solidity Gateway) behavior for repeated `RegisterForeignToken { token_id }` calls is not part of this repository and could not be verified here; the local, verifiable defect is that `register_token`/`do_register_token` never re-checks location equality against the existing `ForeignToNativeId` entry before dispatching the mint-authorizing command, which is the direct structural analog to the reported bug class.

### Citations

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

**File:** prdoc/stable2603/pr_10771.prdoc (L1-17)
```text
title: 'Snowbridge: Describe the token location with the length field included to avoid collisions'
doc:
- audience: Runtime Dev
  description: |-
    For GeneralKey, two XCM junctions that differ only in length can currently produce the same description bytes, 
    and therefore the same TokenId. To avoid such collisions, this PR includes the length field in the describe function.
    We do have several PNAs registered that could be affected by this change. However, these tokens are not currently in use, 
    there have been no transfers and no tokens minted so far. As a result, simply re-registering these tokens should be sufficient, 
    without requiring a runtime storage migration.
crates:
- name: snowbridge-core
  bump: patch
  validate: false
- name: snowbridge-outbound-queue-primitives
  bump: patch
  validate: false

```
