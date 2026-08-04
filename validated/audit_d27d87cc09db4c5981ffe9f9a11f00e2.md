### Title
EIP-2612 permit domain separator binds to a static `ChainId` constant, not to actual chain identity, enabling signature replay across chain forks/clones — (File: `substrate/frame/assets/precompiles/src/permit.rs`)

### Summary
The `permit` pallet (used by the ERC20 precompile's `permit()`/`DOMAIN_SEPARATOR()` functions) computes its EIP-712 domain separator using `T::ChainId::get()`, a compile-time `Get<u64>` runtime constant, exactly mirroring the reported anti-pattern: a value meant to bind signatures to "this chain" that is actually a cached/static configuration constant rather than something derived from the chain's real, unique identity.

### Finding Description
`compute_domain_separator` hashes `DOMAIN_TYPEHASH || name_hash || version_hash || chain_id || verifying_contract`, where `chain_id = T::ChainId::get()`: [1](#0-0) 

`T::ChainId` is declared as a `#[pallet::constant] Get<u64>` in the pallet config: [2](#0-1) 

This is the same class of value as `pallet_revive::Config::ChainId`, which is fixed per runtime binary (e.g. `ConstU64<420_420_421>` for Asset Hub Westend, `ConstU64<420_420_420>` for the Substrate node template): [3](#0-2) [4](#0-3) 

The pallet's own doc comment states the goal explicitly ("preventing replay attacks") but the value used is not the actual live-network identifier: [5](#0-4) 

By contrast, Substrate's own extrinsic-level replay protection (`CheckGenesis`) deliberately avoids this pitfall by binding to the **actual genesis block hash** read from storage at runtime, not a hardcoded constant: [6](#0-5) 

Because `permit::Config::ChainId` (like `pallet_revive::Config::ChainId`) is a Rust-level `Get<u64>` baked into the runtime WASM at compile time, any two chain instances that share the same compiled runtime — testnets reset/relaunched from the same code, forked/cloned networks, or parallel deployments (e.g. local dev chains, chopsticks-style forks, or a westend/rococo-style redeploy) with the same `ChainId` config — will produce byte-for-byte identical `DOMAIN_SEPARATOR()` values. An EIP-712 `permit` signature valid on one such chain instance is therefore also valid on the other, exactly the "signature valid across forks" scenario described in the external report.

### Impact Explanation
A `permit` signature authorizing `owner` to grant `spender` an allowance is a gasless approval that can be submitted by anyone. If the same domain separator is valid across two live chain instances sharing the same `ChainId` constant, a permit signed by a user for use on chain A can be replayed by any third party on chain B to grant/alter ERC20 allowances there, without the user's consent for that specific chain — matching the "theft or unbacked mint/unlock"-class impact via unauthorized approval replay across trust domains that were assumed to be isolated.

### Likelihood Explanation
Exploitation requires two chain instances (e.g. a redeployed testnet, or any environment reusing the same runtime binary/config) to coexist with valid `owner` balances/allowances on both and a `permit` signature obtained from one of them; nonce state also has to line up (a freshly reset chain, or a permit not yet consumed on the target chain, satisfies this trivially). This does not require a malicious validator, collator, relayer, or governance action — a purely public, unprivileged relay of an intercepted signature payload is sufficient once the domain-separator collision condition holds.

### Recommendation
Bind the EIP-712 domain (and thus the permit digest) to a value that uniquely and dynamically identifies the running chain instance, e.g. the genesis block hash (as `frame_system::CheckGenesis` already does) or a runtime-derived unique identifier, rather than solely a compile-time `Get<u64>` constant. At minimum, document and enforce that `ChainId` constants must never be reused across distinct chain deployments, and consider incorporating `frame_system::Pallet::<T>::block_hash(Zero::zero())` into `compute_domain_separator`.

### Proof of Concept
1. Deploy the same runtime binary/config (same `permit::Config::ChainId`) as two separate chain instances — e.g., a testnet reset, a forked/cloned network, or two environments both using the default `ChainId` constant.
2. On chain A, `owner` signs a `permit(owner, spender, value, deadline, v, r, s)` payload; `DOMAIN_SEPARATOR()` is computed via `compute_domain_separator` at [7](#0-6)  using the shared `ChainId`.
3. If `owner` also has funds/allowance state on chain B (same nonce value not yet consumed there), submit the identical `(v, r, s)` to chain B's `permit()` precompile entry point at [8](#0-7) .
4. Because `DOMAIN_SEPARATOR` and `PERMIT_TYPEHASH` are identical and the nonce matches, `use_permit` on chain B validates the signature and grants the allowance, even though the owner never intended to authorize anything on chain B.

### Citations

**File:** substrate/frame/assets/precompiles/src/permit.rs (L81-89)
```rust
	#[pallet::config]
	pub trait Config: frame_system::Config + pallet_timestamp::Config {
		/// The chain ID used in EIP-712 domain separator.
		#[pallet::constant]
		type ChainId: Get<u64>;

		/// Weight information for permit operations.
		type WeightInfo: crate::weights::WeightInfo;
	}
```

**File:** substrate/frame/assets/precompiles/src/permit.rs (L148-178)
```rust
		/// Compute the EIP-712 domain separator for a given verifying contract.
		///
		/// DOMAIN_SEPARATOR = keccak256(abi.encode(
		///   keccak256("EIP712Domain(string name,string version,uint256 chainId,address
		/// verifyingContract)"),
		///   keccak256(name),
		///   keccak256("1"),
		///   chainId,
		///   verifyingContract
		/// ))
		///
		/// The `name` parameter should be the token name per EIP-2612 specification.
		pub fn compute_domain_separator(verifying_contract: &H160, name: &[u8]) -> H256 {
			let name_hash = keccak_256(name);
			let version_hash = keccak_256(b"1");
			let chain_id = T::ChainId::get();

			// Encode: typehash || name_hash || version_hash || chainId || verifyingContract
			let mut data = Vec::with_capacity(DOMAIN_SEPARATOR_ENCODED_LEN);
			data.extend_from_slice(&DOMAIN_TYPEHASH);
			data.extend_from_slice(&name_hash);
			data.extend_from_slice(&version_hash);
			// Pad chain_id to 32 bytes (big-endian)
			data.extend_from_slice(&[0u8; 24]);
			data.extend_from_slice(&chain_id.to_be_bytes());
			// Pad address to 32 bytes
			data.extend_from_slice(&[0u8; 12]);
			data.extend_from_slice(verifying_contract.as_bytes());

			H256(keccak_256(&data))
		}
```

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/src/lib.rs (L1403-1403)
```rust
	type ChainId = ConstU64<420_420_421>;
```

**File:** substrate/bin/node/runtime/src/lib.rs (L1631-1631)
```rust
	type ChainId = ConstU64<420_420_420>;
```

**File:** substrate/frame/revive/src/lib.rs (L326-331)
```rust
		/// The [EIP-155](https://eips.ethereum.org/EIPS/eip-155) chain ID.
		///
		/// This is a unique identifier assigned to each blockchain network,
		/// preventing replay attacks.
		#[pallet::constant]
		type ChainId: Get<u64>;
```

**File:** substrate/frame/system/src/extensions/check_genesis.rs (L27-61)
```rust
/// Genesis hash check to provide replay protection between different networks.
///
/// # Transaction Validity
///
/// Note that while a transaction with invalid `genesis_hash` will fail to be decoded,
/// the extension does not affect any other fields of `TransactionValidity` directly.
#[derive(Encode, Decode, DecodeWithMemTracking, Clone, Eq, PartialEq, TypeInfo)]
#[scale_info(skip_type_params(T))]
pub struct CheckGenesis<T: Config + Send + Sync>(core::marker::PhantomData<T>);

impl<T: Config + Send + Sync> core::fmt::Debug for CheckGenesis<T> {
	#[cfg(feature = "std")]
	fn fmt(&self, f: &mut core::fmt::Formatter) -> core::fmt::Result {
		write!(f, "CheckGenesis")
	}

	#[cfg(not(feature = "std"))]
	fn fmt(&self, _: &mut core::fmt::Formatter) -> core::fmt::Result {
		Ok(())
	}
}

impl<T: Config + Send + Sync> CheckGenesis<T> {
	/// Creates new `TransactionExtension` to check genesis hash.
	pub fn new() -> Self {
		Self(core::marker::PhantomData)
	}
}

impl<T: Config + Send + Sync> TransactionExtension<T::RuntimeCall> for CheckGenesis<T> {
	const IDENTIFIER: &'static str = "CheckGenesis";
	type Implicit = T::Hash;
	fn implicit(&self) -> Result<Self::Implicit, TransactionValidityError> {
		Ok(<Pallet<T>>::block_hash(BlockNumberFor::<T>::zero()))
	}
```

**File:** substrate/frame/assets/precompiles/src/lib.rs (L196-196)
```rust
			IERC20Calls::permit(call) => Self::permit(asset_id, contract_addr, call, env),
```
