### Title
Unsanitized, arbitrary asset `name`/`symbol` accepted by `register_token` allows spoofing of legitimate ERC20 token identity on the Ethereum side of the bridge - ([File: bridges/snowbridge/pallets/system-v2/src/lib.rs])

### Summary
`pallet-snowbridge-system-v2::register_token` (and the analogous `pallet-snowbridge-system-frontend::register_token`) forwards a caller-supplied `AssetMetadata { name, symbol, decimals }` unmodified into a `Command::RegisterForeignToken` that is ABI-encoded and sent to the Ethereum Gateway contract to instantiate a wrapped ERC20 token. The only constraint on `name`/`symbol` is a 32-byte length bound (`BoundedVec<u8, ConstU32<32>>`); there is no check on which characters or content are allowed. Any account whose origin satisfies `FrontendOrigin` (an XCM-location-derived origin, not a privileged governance origin) can therefore register a foreign asset under an arbitrary Polkadot `Location` while giving it the `name`/`symbol` of an existing, well-known token (e.g. `"USDC"`, `"USDT"`, `"DOT"`). This is the same "no sanitization on a string used to construct an externally-interpreted artifact" bug class as the Oraclize report, just with the external consumer being off-chain wallets/exchanges/bridges reading the ERC20 `name()`/`symbol()` instead of the CryptoCompare API. [1](#0-0) 

### Finding Description
`register_token` only validates the origin via `T::FrontendOrigin::ensure_origin(origin)?` (which returns `Success = Location`, i.e. it accepts any XCM-derived location, not a privileged/root check), converts the `asset_id`/`sender` locations, computes a deterministic `token_id` from the *location*, and then directly copies `metadata.name`/`metadata.symbol` into the outbound `Command::RegisterForeignToken`: [2](#0-1) 

The `AssetMetadata` type imposes only a length bound, with no charset/content validation: [3](#0-2) 

The command is then ABI-encoded as raw `String`/`Bytes` fields and delivered to the Ethereum Gateway, which uses it to instantiate/label the wrapped ERC20 contract: [4](#0-3) [5](#0-4) 

Because `token_id` is derived only from the source `Location` (via `TokenIdOf::convert_location`), the protocol treats identity as location-bound and correctly prevents ID collisions between different locations. However, the human-readable `name`/`symbol` fields — the fields actually surfaced to users, wallets, explorers, and DEX front-ends on the Ethereum side — are completely unconstrained content-wise. An attacker who owns/controls any distinct, otherwise-worthless Polkadot asset location (e.g. a custom asset on a parachain they control) can call `register_token` and label their asset `"USDC"`/`"USDC"` or `"DOT"`/`"DOT"`. This is structurally identical to the seed report: a controller-supplied string is concatenated/embedded into an artifact interpreted by an external system without validation, letting the attacker redirect what that external system reports (CryptoCompare price feed in the seed report; ERC20 `name()`/`symbol()` on Ethereum here).

### Impact Explanation
Off-chain consumers of the bridge (wallets, block explorers, DEX listings, bridging UIs) commonly identify ERC20 tokens by `name`/`symbol` rather than by the internal `token_id`/location. A spoofed wrapped-token contract that displays `"USDC"`/`"USDC"` but is backed by an attacker-controlled, worthless Polkadot asset can trick users into acquiring/trading it believing it is the canonical bridged USDC, leading to direct financial loss for end users and reputational/trust damage to the bridge. This matches the impact class of the seed report (external system deceived into acting on the wrong data because of an unsanitized string), scoped to a public, unprivileged entry point (`register_token`), not requiring any malicious validator/relayer/governance actor.

### Likelihood Explanation
`register_token` is reachable by any account whose origin satisfies `FrontendOrigin`, which is designed to accept XCM origins from parachains/accounts (not a governance-gated origin) — this is a normal, expected, permissionless part of the "register a Polkadot-native token" flow described in the pallet's own module docs. No special privileges, race conditions, or malicious infrastructure roles are required — only ownership of an arbitrary distinct asset location and a call with an attacker-chosen `name`/`symbol` string.

### Recommendation
- Short term: Reject `name`/`symbol` values that exactly (or closely, e.g. case-insensitively) match already-registered foreign token symbols/names, or restrict `register_token` to a governance/allow-listed origin for well-known asset classes.
- Long term: Define and enforce an explicit charset/format policy for `name`/`symbol` (e.g. printable ASCII only, no control characters), and consider requiring uniqueness of `symbol` across all `ForeignToNativeId` entries before emitting `Command::RegisterForeignToken`, mirroring the general recommendation from the seed report to validate/sanitize any user-controlled string before it is embedded in an artifact consumed by an external system.

### Proof of Concept
1. Attacker creates a new asset on any parachain reachable via XCM (e.g. `pallet-assets` asset id `999999` on a parachain they control), giving it any arbitrary supply.
2. Attacker sends an XCM `Transact` (or uses whatever mechanism satisfies `FrontendOrigin`) to BridgeHub calling `EthereumSystem::register_token` with:
   - `asset_id` = the Location of their custom asset,
   - `metadata.name` = `b"USD Coin"`,
   - `metadata.symbol` = `b"USDC"`.
3. `register_token` computes a fresh, valid `token_id` for this new location (distinct from the genuine bridged USDC's `token_id`) and emits `Command::RegisterForeignToken { token_id, name: b"USD Coin", symbol: b"USDC", decimals }` — see [6](#0-5)  — with no rejection based on the string content.
4. The Ethereum Gateway deploys/labels a wrapped ERC20 contract with `name() == "USD Coin"`, `symbol() == "USDC"`, distinct in address/`token_id` from the genuine bridged USDC contract but visually indistinguishable to end users, wallets, and listing services.
5. Attacker bridges their worthless custom asset to Ethereum via this newly registered channel and trades/distributes the resulting "USDC"-labeled ERC20, deceiving counterparties who rely on `name`/`symbol` rather than contract address/`token_id`.

### Citations

**File:** bridges/snowbridge/pallets/system-v2/src/lib.rs (L211-248)
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
```

**File:** bridges/snowbridge/primitives/core/src/lib.rs (L166-186)
```rust
/// Metadata to include in the instantiated ERC20 token contract
#[derive(Clone, Encode, Decode, DecodeWithMemTracking, PartialEq, Debug, TypeInfo)]
pub struct AssetMetadata {
	pub name: BoundedVec<u8, ConstU32<METADATA_FIELD_MAX_LEN>>,
	pub symbol: BoundedVec<u8, ConstU32<METADATA_FIELD_MAX_LEN>>,
	pub decimals: u8,
}

#[cfg(any(test, feature = "std", feature = "runtime-benchmarks"))]
impl Default for AssetMetadata {
	fn default() -> Self {
		AssetMetadata {
			name: BoundedVec::truncate_from(vec![]),
			symbol: BoundedVec::truncate_from(vec![]),
			decimals: 0,
		}
	}
}

/// Maximum length of a string field in ERC20 token metada
const METADATA_FIELD_MAX_LEN: u32 = 32;
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

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/message.rs (L228-236)
```rust
			Command::RegisterForeignToken { token_id, name, symbol, decimals } => {
				RegisterForeignTokenParams {
					foreignTokenID: FixedBytes::from(token_id.as_fixed_bytes()),
					name: Bytes::from(name.to_vec()),
					symbol: Bytes::from(symbol.to_vec()),
					decimals: *decimals,
				}
				.abi_encode()
			},
```
