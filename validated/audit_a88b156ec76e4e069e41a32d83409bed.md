### Title
Permit (EIP-2612) domain separator for asset ERC20 precompiles binds only a fixed `ChainId` constant with no genesis/fork binding, enabling signature replay across chain forks - (File: `substrate/frame/assets/precompiles/src/permit.rs`)

### Summary
`pallet-assets-precompiles`'s permit pallet computes its EIP-712 domain separator from a compile-time `Get<u64>` constant `T::ChainId`, the verifying-contract address, and the asset name — never from the chain's genesis hash or any other value that changes if the chain is forked/cloned. This exactly reproduces the LooksRareExchange bug class: a domain separator "fixed at deployment" that cannot be re-derived after a chain split, allowing signatures to be replayed on both resulting chains.

### Finding Description
`compute_domain_separator` builds the domain separator as `keccak256(DOMAIN_TYPEHASH || name_hash || version_hash || chain_id || verifying_contract)` where `chain_id = T::ChainId::get()`: [1](#0-0) 

`T::ChainId` is declared as a pallet configuration constant, `type ChainId: Get<u64>`, fixed by the runtime at compile time, not derived from the chain's genesis hash or any live/forkable chain identity: [2](#0-1) 

By contrast, Substrate's native extrinsic replay protection intentionally binds the signed payload to the actual genesis block hash via `CheckGenesis`, specifically to differentiate one chain instance/fork from another: [3](#0-2) 

The permit digest (`permit_digest`) reuses this same static domain separator and combines it only with the struct hash (owner/spender/value/nonce/deadline) — again nothing chain-fork-specific: [4](#0-3) 

If a chain running this runtime is forked (e.g., a governance-driven chain split, a disaster-recovery fork that preserves state, or any scenario producing two live chains that share the same compiled `ChainId` constant and the same pre-fork state, including the `Nonces` map), a permit signature that a user signed once remains valid and unconsumed on both resulting chains, because:
- the domain separator is identical on both chains (fixed `ChainId`, same `verifying_contract`, same asset `name`),
- the `Nonces` storage value for that `(verifying_contract, owner)` pair is identical on both chains right after the fork point,
- nothing in `use_permit` / `ecrecover` / `is_s_value_valid` checks anything that differs between the two chains.

This mirrors the reported bug precisely: the domain separator's chain-binding value is fixed at "construction" (compile time) and not tied to the live/forked chain, so `SignatureChecker`-style verification (`ecrecover` against the reconstructed digest) succeeds identically on both forks.

### Impact Explanation
A single `permit` signature authorizing a spender allowance can be used once on each surviving chain after a fork, i.e., **duplicate settlement of the same user-signed authorization across two chain instances**. Since `permit` grants ERC20 allowances that a spender can subsequently use to `transferFrom` the owner's asset balance, an attacker (or the intended spender, if compromised) can drain the same approved amount on both forked chains — a duplicate spend enabled purely by the absence of any live chain-fork binding in the signed payload, consistent with the "duplicate settlement or payout" / "theft" impact categories in scope.

### Likelihood Explanation
This requires a chain fork/split scenario (not a malicious peer, validator, or admin) — the same precondition as the original report. Any parachain or Substrate chain using this permit precompile that undergoes a fork retaining state (a legitimate, foreseeable operational event, not an attacker-controlled condition) will have all previously signed, unconsumed permits become replayable across both chains with no additional attacker capability required beyond broadcasting the already-known signature.

### Recommendation
Include a value that is guaranteed to diverge across forks in the EIP-712 domain separator or digest — e.g., mix in `frame_system::Pallet::<T>::block_hash(0)` (the genesis hash, as `CheckGenesis` already does for native extrinsics) or another chain-identity value that is regenerated/checked at runtime rather than fixed as a compile-time `Get<u64>` constant. Alternatively, detect a change in a canonical "chain instance identifier" and invalidate/require re-signing of domain-separator-bound permits, matching the long-term mitigation recommended in the original report.

### Proof of Concept
1. Deploy runtime R with `permit::Config::ChainId = C` and asset precompile at address `V`; owner Alice signs a `permit(owner=Alice, spender=Bob, value=100, nonce=0, deadline=T)` for asset name `N`, producing digest `D = keccak256(0x1901 || DOMAIN_SEPARATOR(C, N, V) || structHash(...))`.
2. The chain running R forks into chain A and chain B at a block after Alice's signature was generated but before it is submitted; both A and B still compile with the same `ChainId = C` constant and both have `Nonces[V][Alice] == 0`.
3. Submit Alice's untouched signature to `use_permit` on chain A: `compute_domain_separator` reproduces the same `DOMAIN_SEPARATOR(C, N, V)`; `ecrecover` succeeds; allowance granted; nonce becomes 1.
4. Submit the identical signature to chain B: `compute_domain_separator` again reproduces the same separator (chain B has the same fixed `ChainId` constant and same pre-fork nonce); `ecrecover` succeeds a second time on the independent chain state; allowance granted again — the single signed authorization is settled twice across the two live chains. [5](#0-4)

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

**File:** substrate/frame/assets/precompiles/src/permit.rs (L160-238)
```rust
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
