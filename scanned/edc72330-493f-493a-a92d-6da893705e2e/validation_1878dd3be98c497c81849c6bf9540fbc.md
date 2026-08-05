### Title
EIP-712 permit domain separator uses a static config constant instead of the chain's actual identity, enabling cross-chain/hard-fork replay of `permit` signatures - (File: `substrate/frame/assets/precompiles/src/permit.rs`)

### Summary
The `pallet_assets` ERC-2612 `permit` precompile computes its EIP-712 domain separator using `T::ChainId::get()`, a compile-time/config constant, exactly the same broken pattern flagged in the external Golom report where `chainId` was baked into `EIP712_DOMAIN_TYPEHASH` once and never revisited. Because the value is a static `Get<u64>` set in the runtime configuration rather than something intrinsically tied to the chain's live identity (e.g. genesis hash), any two deployments (a fork, a testnet clone, a migrated/re-launched chain, or simply a misconfigured sibling parachain) that keep the same `ChainId` constant and the same precompile address will produce an identical domain separator, allowing a `permit` signature collected on one chain to be replayed on the other.

### Finding Description
`compute_domain_separator` builds the EIP-712 domain hash from `DOMAIN_TYPEHASH`, `name_hash`, `version_hash` (`"1"`), `T::ChainId::get()`, and the `verifying_contract` address: [1](#0-0) 

`T::ChainId` is declared as a `#[pallet::constant] type ChainId: Get<u64>` in the pallet config: [2](#0-1) 

This is functionally identical to the Golom bug: the "chain id" component of the EIP-712 domain is not derived from anything intrinsic to the live chain state (no genesis hash, no `frame_system::block_hash(0)`, no spec version, no para ID lookup) - it is a hardcoded constant supplied once by the runtime author, just as Golom's constructor hardcoded `chainid()` once into `EIP712_DOMAIN_TYPEHASH`. The only other domain-binding input is the `verifying_contract` address, which is derived from the precompile's fixed address scheme (`AssetIdExtractor`/`AddressMatcher`) and is therefore also reproducible byte-for-byte on a fork or clone of the same runtime.

`use_permit`/`do_verify_permit` never check anything else that would differentiate one chain instance from another (no genesis hash, no runtime spec-version check): [3](#0-2) 

Substrate's native extrinsic signing already has a dedicated mechanism for this problem — `frame_system::CheckGenesis`/`CheckSpecVersion` bind a signed payload to the chain's genesis hash, so that a hard fork or a state-migrated re-launch (which by definition produces a new genesis block) invalidates old signatures. The EIP-712 permit path in this precompile bypasses that protection entirely and reintroduces the exact same static-value replay class that the Golom report warned about, but for gasless ERC-20 approvals routed through `pallet-revive`.

### Impact Explanation
Any parachain, testnet, or migrated instance that reuses the same `ChainId` config constant (a very plausible operational scenario — teams frequently reuse the same value across a mainnet and its testnet clone, or forget to bump it after a chain migration/hard fork) will accept `permit` signatures that were generated for a different, distinct chain deployment. An attacker who obtains a signed permit intended for chain A can replay it against chain B (or the post-fork continuation of the same chain) to gain unauthorized ERC-20 `approve` rights over the victim's assets on that second chain, leading to unauthorized allowance grants and potential subsequent theft via the granted spender allowance. This falls squarely in the "forged or mis-bound proof/state acceptance" and "unauthorized execution" impact classes.

### Likelihood Explanation
The nonce (`Nonces` double map) only prevents replay *within* a single chain instance, since nonces are chain-local storage; it does nothing to prevent replay across a fork or a cloned chain that shares the same `ChainId` and precompile address. The attack requires no privileged access, malicious validator, or governance action — it only requires an unprivileged relayer/attacker to capture a previously-issued, publicly visible signature (permits are typically broadcast off-chain before submission) and rebroadcast it against the second chain. The precondition (identical `ChainId` constant across two live deployments) is an ordinary runtime-configuration choice, not an exotic attacker capability, making this a realistic operational risk whenever a chain is forked, migrated, or cloned for testing/staging with the same config value.

### Recommendation
Bind the domain separator to something intrinsically unique per chain instance rather than (or in addition to) a static config constant — e.g., incorporate `frame_system::Pallet::<T>::block_hash(BlockNumber::zero())` (the genesis hash) or the runtime `spec_version`/`transaction_version` into the EIP-712 domain, mirroring what `CheckGenesis`/`CheckSpecVersion` already do for native extrinsics. At minimum, document and enforce that `ChainId` must be globally unique per deployment and recompute/verify it is never reused across forked or cloned runtimes.

### Proof of Concept
1. Deploy runtime `A` with `pallet_assets_precompiles::Config::ChainId = X` and a given precompile address for a given asset id.
2. Fork or clone the chain into runtime `B` with the same `pallet_assets_precompiles::Config::ChainId = X` (e.g., a hard fork, a staging clone, or a migrated re-launch reusing the runtime config) and the same asset id → identical precompile address.
3. A user signs an EIP-712 `Permit(owner, spender, value, nonce=0, deadline)` for chain A's precompile.
4. The signature's digest is `keccak256("\x19\x01" || domain_separator || struct_hash)`, where `domain_separator` depends only on `DOMAIN_TYPEHASH`, token name, `"1"`, `ChainId::get()==X`, and the identical `verifying_contract` address — identical on both A and B.
5. Because chain B has never seen this nonce consumed, submitting the same `(v, r, s)` to chain B's `use_permit` passes `do_verify_permit` and grants the spender an allowance on chain B that the owner never authorized there. [4](#0-3)

### Citations

**File:** substrate/frame/assets/precompiles/src/permit.rs (L82-89)
```rust
	pub trait Config: frame_system::Config + pallet_timestamp::Config {
		/// The chain ID used in EIP-712 domain separator.
		#[pallet::constant]
		type ChainId: Get<u64>;

		/// Weight information for permit operations.
		type WeightInfo: crate::weights::WeightInfo;
	}
```

**File:** substrate/frame/assets/precompiles/src/permit.rs (L160-178)
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
```

**File:** substrate/frame/assets/precompiles/src/permit.rs (L311-362)
```rust
		fn do_verify_permit(
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
			// EIP-2612: owner and spender cannot be the zero address
			if owner.is_zero() {
				return Err(Error::<T>::InvalidOwner);
			}
			if spender.is_zero() {
				return Err(Error::<T>::InvalidSpender);
			}

			// Validate deadline against current timestamp.
			// EIP-2612 specifies deadlines in UNIX seconds. We use the `UnixTime`
			// trait which returns a `core::time::Duration` — its `as_secs()` method
			// gives us seconds regardless of pallet_timestamp's internal resolution
			// (which stores milliseconds, converted via `Duration::from_millis` in
			// pallet_timestamp's `UnixTime` implementation).
			let now_seconds = <pallet_timestamp::Pallet<T> as UnixTime>::now().as_secs();
			let deadline_u256 = U256::from_big_endian(deadline);
			let now_u256 = U256::from(now_seconds);

			if deadline_u256 < now_u256 {
				return Err(Error::<T>::PermitExpired);
			}

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

			Ok(())
		}
```

**File:** substrate/frame/assets/precompiles/src/permit.rs (L374-403)
```rust
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
