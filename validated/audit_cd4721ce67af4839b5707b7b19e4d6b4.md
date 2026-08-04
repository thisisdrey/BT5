Found a real hardcoded-value mismatch analog: `pallet-assets-precompiles`' EIP-712 domain separator binds a `Config::ChainId` constant that is decoupled from the runtime's actual EVM chain ID (`pallet_revive::Config::ChainId`), the same class of "identifier that must match a signature/consumer but is asserted as a separate hardcoded constant" bug as the ERC865 4-byte selector issue.

### Title
EIP-712 permit domain separator uses an independently hardcoded `ChainId` that can diverge from `pallet_revive`'s actual chain id, enabling cross-network permit replay - (File: substrate/frame/assets/precompiles/src/permit.rs)

### Summary
The ERC865 bug was about a manually-maintained 4-byte identifier drifting from the value actually produced by the real function signature, breaking the invariant that the identifier must exactly and uniquely bind to what it is supposed to represent. The local analog is `pallet_assets_precompiles::permit::Pallet::compute_domain_separator`, which binds an EIP-712 permit signature to `T::ChainId` — a pallet-local constant configured independently of `pallet_revive::Config::ChainId`, the value that actually determines the chain's EVM identity used everywhere else (eth_call, tx signing domain, `chainId()` RPC). Nothing in the code enforces these two constants stay equal.

### Finding Description
`DOMAIN_TYPEHASH`/`PERMIT_TYPEHASH` are computed correctly at compile time via `const_crypto::sha3::Keccak256` [1](#0-0) , so unlike the ERC865 case the typehash itself is safe. However, `compute_domain_separator` folds in `T::ChainId::get()` as the chain-binding component of the EIP-712 domain separator: [2](#0-1) 

`Config::ChainId` for this pallet is a completely separate config item from `pallet_revive::Config::ChainId`, which is the pallet that actually assigns the runtime its Ethereum-visible chain id (used for EVM RPC `eth_chainId`, transaction signing domain, etc.): [3](#0-2) 

There is no compile-time or runtime assertion that `pallet_assets_precompiles::PermitConfig::ChainId == pallet_revive::Config::ChainId`; each runtime integrator sets both constants by hand. This is exactly the ERC865 pattern: a hash/identifier that is supposed to unambiguously and permanently bind to one canonical value (`the real signature`/`the real chain id`) but is instead re-derived from a separately maintained literal that can silently mismatch its intended target — with no test or build-time check preventing the drift.

### Impact Explanation
If a runtime's `permit::Config::ChainId` is misconfigured to a value shared by multiple deployed chains (e.g., a template default, a value copied from another network's runtime code, or simply left unpatched when the network is redeployed/forked for testing), an EIP-2612 `permit` signature signed by a token owner for use on chain A becomes valid and replayable on chain B running the same pallet code, because the EIP-712 digest computed by `permit_digest` is identical on both chains. `use_permit` only checks nonce, deadline and signer recovery — none of which detect cross-chain replay if the domain separator matches: [4](#0-3)  This allows an attacker who intercepts (or is simply given, e.g. via a dApp) a permit signature on one network to call `permit()`+`transferFrom()` on the token holder's assets on another network sharing the mismatched `ChainId`, granting unauthorized ERC20 allowance/spend without the owner's consent on that second network — theft of approved allowance / unauthorized execution against asset holdings.

### Likelihood Explanation
This requires no malicious validator, relayer, or governance action — only a runtime configuration slip (e.g., copy-pasting a template runtime's `PermitConfig::ChainId` constant into a new deployment, or a testnet/mainnet pair reusing the same value while each also independently sets a distinct `pallet_revive::Config::ChainId`). Nothing in the codebase enforces the two chain-id constants are tied together, and the `substrate/frame/assets/precompiles/src/mock.rs` and kitchensink example both hardcode `ChainId` as a bare `ConstU64` literal, showing the pattern that will be copied into further runtime integrations without any coupling check.

### Recommendation
Remove the independent `permit::Config::ChainId` associated type and instead derive the EIP-712 domain separator's chain id directly from `pallet_revive::Config::ChainId` (require `Runtime: pallet_revive::Config` in `permit::Config` and call `<Runtime as pallet_revive::Config>::ChainId::get()`), or add a compile-time/genesis-build assertion that the two constants are equal, so the signature domain can never silently diverge from the network's real, externally-observed chain id.

### Proof of Concept
1. Deploy the same runtime code (with `pallet_assets_precompiles` + `pallet_revive`) on network A with `pallet_revive::Config::ChainId = 420_420_421` but `permit::Config::ChainId` left at a template default (e.g. `420_420_420`), and on network B with a different `pallet_revive::Config::ChainId` but the same unpatched `permit::Config::ChainId` default.
2. On network A, a token owner signs an EIP-2612 `permit(owner, spender, value, deadline, v, r, s)` for a given ERC20 precompile address.
3. Because `compute_domain_separator` only depends on `permit::Config::ChainId` (identical on A and B) and the precompile address/name (same asset metadata replicated across both networks), the digest is identical.
4. Submit the same `(v, r, s)` to the ERC20 precompile's `permit()` entry point on network B via `raw_permit`/`use_permit`; signature recovery succeeds and `spender`'s allowance is granted on network B, which the owner never authorized on that network.

### Citations

**File:** substrate/frame/assets/precompiles/src/permit.rs (L40-53)
```rust
/// EIP-712 type hash for the domain separator.
/// keccak256("EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)")
pub(crate) const DOMAIN_TYPEHASH: [u8; 32] = const_crypto::sha3::Keccak256::new()
	.update(b"EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)")
	.finalize();

/// EIP-712 type hash for Permit.
/// keccak256("Permit(address owner,address spender,uint256 value,uint256 nonce,uint256 deadline)")
///
/// Computed at compile time from the canonical string, eliminating any risk of a
/// copy-paste error in a hand-written byte array.
pub(crate) const PERMIT_TYPEHASH: [u8; 32] = const_crypto::sha3::Keccak256::new()
	.update(b"Permit(address owner,address spender,uint256 value,uint256 nonce,uint256 deadline)")
	.finalize();
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

**File:** substrate/bin/node/runtime/src/lib.rs (L1538-1541)
```rust
impl pallet_assets_precompiles::PermitConfig for Runtime {
	type ChainId = ConstU64<420_420_420>;
	type WeightInfo = pallet_assets_precompiles::weights::SubstrateWeight<Runtime>;
}
```
