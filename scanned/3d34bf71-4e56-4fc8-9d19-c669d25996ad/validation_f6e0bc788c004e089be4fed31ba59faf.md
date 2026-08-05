## Title
EIP-2612 permit domain separator binds only to `pallet_revive::Config::ChainId`, not to the chain's genesis/instance identity, enabling cross-deployment signature replay for gasless approvals - (File: `substrate/frame/assets/precompiles/src/permit.rs`)

### Summary
The `pallet_assets_precompiles` ERC-2612 `permit` implementation computes its EIP-712 domain separator from `T::ChainId` (a compile-time constant configured per runtime), the token name, and the precompile (verifying-contract) address [1](#0-0) . In the Asset Hub Westend runtime this `ChainId` is wired straight to `<Runtime as pallet_revive::Config>::ChainId` [2](#0-1) . This mirrors exactly the root cause in the Beanstalk report: a signature-verification domain that is supposed to be unique per network is instead a fixed, copy-pasted constant, so any two Substrate-based chains (testnets, forks, or chains that reuse default configuration) that end up with the same `pallet_revive::Config::ChainId` value share an identical domain separator for a given asset/precompile address+name. A permit signed for use on chain A can then be replayed verbatim on chain B.

### Finding Description
The permit digest is built as:
```
digest = keccak256("\x19\x01" || domainSeparator || structHash)
domainSeparator = keccak256(DOMAIN_TYPEHASH || keccak256(name) || keccak256("1") || chainId || verifyingContract)
structHash = keccak256(PERMIT_TYPEHASH || owner || spender || value || nonce || deadline)
``` [3](#0-2) 

The only network-binding component is `chainId`, sourced from `T::ChainId: Get<u64>` [4](#0-3) . Unlike Ethereum L1/L2 chain IDs (globally registered, hard to collide), `pallet_revive::Config::ChainId` is a locally-chosen constant set independently by each Substrate-based chain's runtime developers. There is no mechanism tying this value to the chain's genesis hash, spec name/version, or any other globally-unique network fingerprint — the same value substrate node runtime, `asset-hub-westend`, `people-westend`, dev/test networks, or any forked/derivative chain built from this codebase can (and, per the test mock, commonly does for local/dev setups) configure the same `ChainId` (e.g., the widely-used default `31337`, seen in the pallet's own test mock) [5](#0-4) .

Replay protection for a *single* chain relies on `Nonces<T>` per `(verifying_contract, owner)` and is correctly consumed atomically in `use_permit` [6](#0-5) . However, nonce state is chain-local storage — it does not exist, or starts at zero, on a second chain instance that shares the same `ChainId`, `verifyingContract` address (deterministic from the asset ID/precompile-prefix scheme), and token `name`. Because none of `domainSeparator`'s inputs (`name`, `chainId`, `verifyingContract`) nor `structHash`'s inputs include a genesis hash or any other chain-instance-unique value, an owner's signature produced for chain A's asset precompile is byte-for-byte valid on chain B's identically-configured instance, where the nonce for that owner is still `0`.

This directly parallels the reported flaw: "the current implementation ... does not include the chainId in the hash computation, making it possible for an attacker to replay the same transaction on different chains," except here the analog is "the chain-identifying value used is not actually unique across deployments," which produces the same practical outcome — the signature verification cannot distinguish between two different chain instances.

### Impact Explanation
If an attacker obtains a permit signature intended for use on one chain (e.g., a testnet, staging deployment, or any fork of the runtime using the same `ChainId` constant), they can submit that identical `(v, r, s)` permit via the `permit()` precompile entrypoint on the other chain to grant themselves (or any `spender`) an ERC-20 allowance over the victim owner's asset balance on that chain, without the owner's consent for that specific chain — a form of unauthorized approval/theft of asset allowance, matching the "theft or unbacked mint or unlock" and "unauthorized execution" categories in scope. Because `permit()` is a public, unprivileged precompile entrypoint reachable by any caller with no signer cooperation needed beyond the leaked/observed signature, this is exploitable by any unprivileged party who has seen a valid permit signature on one network.

### Likelihood Explanation
Likelihood depends on how many independently-deployed runtimes derived from this codebase reuse the same `ChainId` constant (this is a realistic risk since `pallet_revive::Config::ChainId` is a plain `Get<u64>` parameter with no uniqueness enforcement, and the pallet's own test/mock configuration models exactly this kind of static, easily-duplicated value). It is a systemic footgun rather than a certainty on any two specific named networks, since it requires two runtimes to actually collide on `ChainId` — but nothing in the SDK prevents or warns against this collision, and forked/testnet/devnet chains built from the same base runtime are the most likely victims.

### Recommendation
Bind the EIP-712 domain separator (or the permit struct hash) to a value that is guaranteed unique per chain instance — e.g., include the chain's genesis hash (as `frame_system::Pallet::<T>::block_hash(0)`, analogous to `CheckGenesis`'s replay-protection mechanism already used elsewhere in this codebase [7](#0-6) ) — instead of, or in addition to, the runtime-configured `ChainId`. Alternatively, enforce/document that `pallet_revive::Config::ChainId` must be globally unique per network and never reused across forks/testnets, and add a defensive check that fails obviously-reused defaults (e.g. `31337`) in production `TestDefaultConfig`-derived runtimes.

### Proof of Concept
1. Deploy runtime R1 with `pallet_revive::Config::ChainId = X` and asset A at precompile address `P`, token name `N`.
2. Deploy runtime R2 (fork/testnet using the same base code) that is also configured with `ChainId = X` (e.g. both left at a shared default, or copy-pasted parameter), with the same asset creation sequence producing the same precompile address `P` and same token `name` `N`.
3. Owner O signs a permit for `(owner=O, spender=S, value=V, nonce=0, deadline=D)` intended for use on R1's asset A, producing `(v, r, s)` per `permit_digest` [8](#0-7) .
4. On R2, before O has ever called `permit()` there (nonce is still `0`), an attacker submits `permit(owner=O, spender=S, value=V, deadline=D, v, r, s)` against R2's asset-A precompile at address `P`.
5. `do_verify_permit` recomputes the same digest (same `name`, `chainId=X`, `verifyingContract=P`, `nonce=0`) and `ecrecover` returns `O`, so the signature check passes [9](#0-8) ; `use_permit` increments R2's nonce and grants `S` the allowance `V` over O's assets on R2 — an unauthorized allowance the owner never intended for that chain.

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

**File:** substrate/frame/assets/precompiles/src/permit.rs (L180-238)
```rust
		/// Compute the EIP-712 struct hash for a permit.
		///
		/// structHash = keccak256(abi.encode(
		///   PERMIT_TYPEHASH,
		///   owner,
		///   spender,
		///   value,
		///   nonce,
		///   deadline
		/// ))
		pub fn permit_struct_hash(
			owner: &H160,
			spender: &H160,
			value: &[u8; 32], // U256 as bytes (big-endian)
			nonce: &U256,
			deadline: &[u8; 32], // U256 as bytes (big-endian)
		) -> H256 {
			let mut data = Vec::with_capacity(PERMIT_STRUCT_ENCODED_LEN);
			data.extend_from_slice(&PERMIT_TYPEHASH);
			// owner (padded to 32 bytes)
			data.extend_from_slice(&[0u8; 12]);
			data.extend_from_slice(owner.as_bytes());
			// spender (padded to 32 bytes)
			data.extend_from_slice(&[0u8; 12]);
			data.extend_from_slice(spender.as_bytes());
			// value (already 32 bytes)
			data.extend_from_slice(value);
			// nonce (convert U256 to 32 bytes big-endian)
			data.extend_from_slice(&nonce.to_big_endian());
			// deadline (already 32 bytes)
			data.extend_from_slice(deadline);

			H256(keccak_256(&data))
		}

		/// Compute the final EIP-712 digest to be signed.
		///
		/// digest = keccak256("\x19\x01" || domainSeparator || structHash)
		///
		/// The `name` parameter should be the token name per EIP-2612 specification.
		pub fn permit_digest(
			verifying_contract: &H160,
			name: &[u8],
			owner: &H160,
			spender: &H160,
			value: &[u8; 32],
			nonce: &U256,
			deadline: &[u8; 32],
		) -> [u8; 32] {
			let domain_separator = Self::compute_domain_separator(verifying_contract, name);
			let struct_hash = Self::permit_struct_hash(owner, spender, value, nonce, deadline);

			let mut data = Vec::with_capacity(DIGEST_PREFIX_LEN);
			data.extend_from_slice(&[0x19, 0x01]);
			data.extend_from_slice(domain_separator.as_bytes());
			data.extend_from_slice(struct_hash.as_bytes());

			keccak_256(&data)
		}
```

**File:** substrate/frame/assets/precompiles/src/permit.rs (L344-359)
```rust
			let nonce = Self::nonce(verifying_contract, owner);
			let digest = Self::permit_digest(
				verifying_contract,
				name,
				owner,
				spender,
				value,
				&nonce,
				deadline,
			);

			let recovered = Self::ecrecover(&digest, v, r, s)?;

			if &recovered != owner {
				return Err(Error::<T>::SignerMismatch);
			}
```

**File:** substrate/frame/assets/precompiles/src/permit.rs (L364-403)
```rust
		/// Verify and consume a permit signature atomically.
		///
		/// This is the recommended function for production use. It:
		/// 1. Validates the deadline against the current timestamp
		/// 2. Verifies the signature matches the owner
		/// 3. Increments the nonce to prevent replay attacks
		///
		/// The `name` parameter should be the token name per EIP-2612 specification.
		///
		/// After this function returns `Ok(())`, the permit cannot be used again.
		pub fn use_permit(
			verifying_contract: &H160,
			name: &[u8],
			owner: &H160,
			spender: &H160,
			value: &[u8; 32],
			deadline: &[u8; 32],
			v: u8,
			r: &[u8; 32],
			s: &[u8; 32],
		) -> Result<(), Error<T>> {
			// Verify the permit first
			Self::do_verify_permit(
				verifying_contract,
				name,
				owner,
				spender,
				value,
				deadline,
				v,
				r,
				s,
			)?;

			// Consume the permit by incrementing the nonce
			// This prevents the same permit from being used again
			Self::increment_nonce(verifying_contract, owner)?;

			Ok(())
		}
```

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/src/lib.rs (L617-620)
```rust
impl pallet_assets_precompiles::PermitConfig for Runtime {
	type ChainId = <Runtime as pallet_revive::Config>::ChainId;
	type WeightInfo = pallet_assets_precompiles::weights::SubstrateWeight<Runtime>;
}
```

**File:** substrate/frame/assets/precompiles/src/mock.rs (L96-105)
```rust
parameter_types! {
	/// Test chain ID - use a distinct value to avoid masking chain ID handling bugs.
	/// 31337 is commonly used for local development chains (Hardhat default).
	pub const ChainId: u64 = 31337;
}

impl permit::pallet::Config for Test {
	type ChainId = ChainId;
	type WeightInfo = ();
}
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
