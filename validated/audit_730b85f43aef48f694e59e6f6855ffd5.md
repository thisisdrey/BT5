### Title
EIP-2612 permit domain separator binds to a static config constant, not the actual chain identity, enabling permit replay across forked chains - (File: `substrate/frame/assets/precompiles/src/permit.rs`)

### Summary
The `pallet_assets_precompiles` permit pallet implements EIP-2612 gasless approvals via EIP-712 signatures. Its `compute_domain_separator` binds the signature only to a compile-time/genesis-config constant `T::ChainId` rather than to any value that actually changes when the chain is forked (e.g. genesis hash). This mirrors exactly the reported bug class: "DOMAIN_SEPARATOR stored as immutable" / independent of the real chain identity, enabling replay of signed messages on forked networks.

### Finding Description
`compute_domain_separator` builds the EIP-712 domain hash using `T::ChainId::get()`: [1](#0-0) 

`ChainId` is declared as a `#[pallet::constant] type ChainId: Get<u64>` in the pallet config: [2](#0-1) 

This is a fixed constant supplied by the runtime at compile/config time — analogous to Solidity `immutable` state cached once — and is completely decoupled from the actual on-chain genesis/network identity that Substrate uses elsewhere for replay protection. Ordinary signed extrinsics are protected against cross-network replay by `frame_system::CheckGenesis`, which binds the signature to `Pallet::<T>::block_hash(0)` (the genesis hash) as part of the transaction's `Implicit` data: [3](#0-2) 

The permit signature flow, however, does not go through `TransactionExtension`/`CheckGenesis` at all — it is a raw off-chain ECDSA/EIP-712 signature submitted as calldata to the `permit()` precompile function, verified purely by `permit_digest`/`compute_domain_separator` and the per-`(verifying_contract, owner)` nonce: [4](#0-3) 

Because the only chain-binding element in the digest is the static `T::ChainId` constant (and the `verifying_contract` address, which is derived deterministically from `asset_id`, not from genesis state), any two chain instances that share the same runtime binary/config — e.g. a forked testnet, a rolled-back or restarted network with the same genesis config but different genesis hash, or a deliberately cloned chain — will compute an identical domain separator and therefore accept the exact same signed permit. The nonce state (`Nonces` storage map) also resets to zero on a genesis fork, so the "atomically verify and consume" replay protection in `use_permit`/`increment_nonce` does not help across forked instances: [5](#0-4) 

The comment in the module even documents "Domain separation: Each verifying contract has its own domain separator" but never claims binding to genesis/network identity: [6](#0-5) 

### Impact Explanation
An owner's previously-signed EIP-2612 `permit` (an off-chain message granting an ERC-20 allowance to a spender) that has been used or is still pending on the original network can be resubmitted verbatim on any forked instance of the same runtime (test fork, chain restart from a snapshot, migration rehearsal network, or any environment sharing the `ChainId` constant and asset id layout). This grants the spender an allowance the owner never intended to authorize on that specific chain instance, which combined with the spender's `transferFrom` capability leads to unauthorized token transfer/allowance theft — an origin-escalation / unauthorized-execution style outcome achieved purely by an unprivileged actor replaying a signature, with no relayer, admin, or validator collusion required.

### Likelihood Explanation
Likelihood is moderate: it requires the existence of a second chain instance sharing the identical `ChainId` constant (same runtime build/config) — which routinely happens with public testnets that get forked/rolled back, disaster-recovery restarts from genesis snapshots, or parallel staging/testing networks built from the same runtime code. No modification of governance, no leaked keys, and no malicious infrastructure actor is needed; the attacker only needs to capture a previously broadcast/signed permit payload and resubmit it on the sibling network.

### Recommendation
Bind the EIP-712 domain separator to a value that is unique per genesis/chain instance rather than a static config constant — e.g. incorporate `frame_system::Pallet::<T>::block_hash(0)` (the genesis hash, the same primitive `CheckGenesis` already uses for extrinsic replay protection) into `compute_domain_separator`, or recompute/verify the domain separator dynamically per call instead of caching a single runtime-wide constant. This follows the same remediation recommended in the original report: derive the chain-binding component from live/genesis chain state rather than an immutable value fixed independently of the actual network.

### Proof of Concept
1. Deploy runtime `R` with `permit::Config::ChainId = X` on network `A`; asset `N` is created deterministically at address `addr`.
2. Owner `O` signs an EIP-2612 permit `(owner=O, spender=S, value=V, nonce=0, deadline=D)` against `compute_domain_separator(addr, name)`, which only depends on `DOMAIN_TYPEHASH`, `name`, `"1"`, `X`, and `addr` — none of which differ between networks with the same config.
3. Operator forks/restarts the chain (or spins up a staging network) from the same runtime binary, producing network `B` with a different genesis hash but identical `ChainId = X` and identical asset creation sequence (same `asset_id` → same `addr`).
4. Nonce for `(addr, O)` is `0` again on network `B` (state was not carried over, or the fork occurred pre-permit-usage).
5. Attacker (or spender `S`) submits the exact same `(v, r, s)` permit calldata to `permit()` on network `B`. `permit_digest` recomputes an identical hash to what was signed on network `A`, ECDSA recovery returns `O`, nonce check passes, and the allowance is granted on network `B` without `O`'s intent for that network — as verified in [7](#0-6) , which only tests same-network nonce replay, not cross-network domain binding.

### Citations

**File:** substrate/frame/assets/precompiles/src/permit.rs (L23-30)
```rust
//! # Security Notes
//!
//! - **Nonce management**: Use `use_permit` (not `verify_permit`) to atomically verify and consume
//!   permits. This prevents replay attacks.
//! - **Deadline validation**: Permits are validated against UNIX timestamps.
//! - **Domain separation**: Each verifying contract has its own domain separator.
//! - **Signature malleability**: The `s` value is checked to be in the lower half of the secp256k1
//!   curve order to prevent signature malleability attacks.
```

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

**File:** substrate/frame/assets/precompiles/src/permit.rs (L94-110)
```rust
	/// Nonces for permit signatures.
	/// Mapping: (verifying_contract, owner_address) => nonce
	///
	/// Uses Blake2_128Concat for the first key to prevent storage collision attacks
	/// when the verifying_contract address could be influenced by an attacker.
	///
	/// Note: EIP-2612 specifies uint256 nonce. We store as U256 for compatibility.
	#[pallet::storage]
	pub type Nonces<T: Config> = StorageDoubleMap<
		_,
		Blake2_128Concat,
		H160, // verifying contract address (precompile address)
		Blake2_128Concat,
		H160, // owner ethereum address
		U256, // nonce (EIP-2612 uses uint256)
		ValueQuery,
	>;
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

**File:** substrate/frame/assets/precompiles/src/permit.rs (L215-238)
```rust
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

**File:** substrate/frame/system/src/extensions/check_genesis.rs (L56-61)
```rust
impl<T: Config + Send + Sync> TransactionExtension<T::RuntimeCall> for CheckGenesis<T> {
	const IDENTIFIER: &'static str = "CheckGenesis";
	type Implicit = T::Hash;
	fn implicit(&self) -> Result<Self::Implicit, TransactionValidityError> {
		Ok(<Pallet<T>>::block_hash(BlockNumberFor::<T>::zero()))
	}
```

**File:** substrate/frame/assets/precompiles/src/permit_precompile_tests.rs (L512-524)
```rust
/// Replay of a consumed permit must fail through the precompile path.
/// EIP-2612's headline guarantee — `permit()` atomically verifies and
/// consumes a signature — is realized by `use_permit` incrementing the
/// nonce. A regression that swapped `use_permit` for `verify_permit`
/// (which does NOT bump the nonce) would pass every other test in this
/// submodule. Pinning the invariant at the precompile layer.
///
/// First submission consumes the permit (nonce 0 → 1). The same
/// `(v, r, s)` is then re-submitted; the precompile re-derives the
/// digest using the new on-chain nonce, recovery yields a different
/// signer, and `recovered != owner` fires "Signer does not match
/// owner".
#[test]
```
