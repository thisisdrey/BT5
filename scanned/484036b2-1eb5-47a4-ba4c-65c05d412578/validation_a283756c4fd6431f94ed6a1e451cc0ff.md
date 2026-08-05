Based on the investigation, the strongest local analog to the reported "missing chain-id-in-hash → cross-chain signature replay" bug class is in the new `pallet_assets_precompiles`'s EIP-2612 `permit` implementation, not in the bridges/messages code (which already binds lane, nonce and proof to specific chains via `LaneId`/`ChainId` constants and has dedicated replay-rejecting extensions).

### Title
EIP-712 Permit domain separator binds to a config-time `ChainId` constant, not the chain's actual genesis/identity, enabling replay across differently-deployed chains sharing the same runtime code - (File: substrate/frame/assets/precompiles/src/permit.rs)

### Summary
`pallet_assets_precompiles::permit` implements an EIP-2612-style permit for asset approvals via secp256k1 signatures, following the exact same "hash inputs must be unique per-chain" pattern flagged in the external report. The domain separator explicitly includes `chainId`, but that value is `T::ChainId::get()` — a pallet `Config::ChainId: Get<u64>` constant fixed by the runtime at compile/config time (`ConstU64<...>`), rather than being derived from anything cryptographically tied to that specific chain's live identity (e.g. genesis hash or SS58 network id). [1](#0-0) [2](#0-1) 

### Finding Description
The EIP-712 digest is built as `keccak256("\x19\x01" || domainSeparator || structHash)`, where `domainSeparator = keccak256(DOMAIN_TYPEHASH || name_hash || version_hash || chainId || verifyingContract)` [3](#0-2) . This mirrors exactly the mitigation recommended in the external report (embed chain id in the signed hash) — but the value bound here is a static `Get<u64>` config constant supplied by the runtime, not a property that is unique to a specific running chain. Any two Substrate-based chains that reuse the same runtime crate configuration (a common pattern: shared runtime code across a canary/testnet and mainnet-style deployment, or a chain that is later forked/relaunched with fresh genesis but identical `Config` wiring) will produce the *same* `ChainId` constant, the *same* `verifying_contract` (deterministic precompile address derived from the asset id) and the *same* `name`/nonce state (`Nonces` starts at zero on both chains) [4](#0-3) . In that scenario, a permit signature produced by an owner for Chain A is also a valid, unconsumed permit on Chain B, letting an attacker replay the exact same `(v,r,s)` to grant themselves the same allowance on the second chain — the identical bug class as the report's `borrowHash` missing `block.chainid` binding, except here the "chain id" field exists but is not chain-unique. `use_permit` only guards against *same-chain* replay by incrementing `Nonces` [5](#0-4) ; it does nothing to prevent cross-chain replay when the domain separator's `chainId` input collides between deployments.

### Impact Explanation
If exploited, an attacker who obtains (or is given, e.g. via a dApp integration) a permit signature intended for use on one chain deployment can replay it on another chain configured with the same `ChainId` constant to obtain unauthorized `Assets` allowance for the `spender`, i.e. unauthorized-execution / origin-escalation on the allowance and potential subsequent asset drain via the granted allowance. This falls within the "forged or mis-bound proof or state acceptance" / "unauthorized execution" impact categories of the gate.

### Likelihood Explanation
Likelihood depends entirely on whether any two live deployments derived from this runtime code configure the same `ChainId` constant value while also sharing predictable `verifying_contract` addresses — this is plausible because `ChainId` here is a hand-set config constant (unlike an EVM chain's canonical, network-specific chain id) and Substrate/Polkadot SDK runtimes are frequently reused across networks (test networks, canary networks, chain relaunches) with identical pallet configuration. I could not fully verify from the index the exact `ChainId` constant values wired into `asset-hub-westend` and `penpal` runtimes to confirm an existing real-world collision (searches confirmed both runtimes reference `ChainId` configuration but line-level values were not retrievable within this session) — this should be checked directly to confirm whether any current or template deployments already collide.

### Recommendation
Derive the EIP-712 `chainId` field from a value cryptographically unique to the running chain instance — e.g. the genesis hash (as `frame_system::Pallet::<T>::block_hash(0)`, the same value already used by `CheckGenesis` for transaction replay protection [6](#0-5) ) or a runtime-unique identifier that cannot collide across independently-launched chains, instead of (or in addition to) a hand-configured `Get<u64>` constant.

### Proof of Concept
1. Deploy two Substrate chains (Chain A and Chain B) both using `pallet_assets_precompiles` with identical `Config::ChainId` constants (e.g. both left at the default/template value).
2. On Chain A, asset owner signs a permit for `spender`/`value`/`deadline` at nonce 0 for asset `verifying_contract` X; `use_permit` is called, consuming nonce 0 on Chain A.
3. Because `Nonces` storage is chain-local, the same owner's nonce for that `verifying_contract` on Chain B is still 0 — replay the identical `(v,r,s)` via `use_permit` on Chain B; `permit_digest` recomputes to the same value (same `chainId`, `verifying_contract`, `name`, `nonce=0`), `ecrecover` returns the same owner, and the permit succeeds, granting the same allowance a second time on a different chain without the owner's fresh consent.

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

**File:** substrate/frame/system/src/extensions/check_genesis.rs (L56-61)
```rust
impl<T: Config + Send + Sync> TransactionExtension<T::RuntimeCall> for CheckGenesis<T> {
	const IDENTIFIER: &'static str = "CheckGenesis";
	type Implicit = T::Hash;
	fn implicit(&self) -> Result<Self::Implicit, TransactionValidityError> {
		Ok(<Pallet<T>>::block_hash(BlockNumberFor::<T>::zero()))
	}
```
