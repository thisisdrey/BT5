### Title
Fork/clone replay of ERC20Permit signatures due to static `ChainId` config constant not bound to genesis hash — (File: `substrate/frame/assets/precompiles/src/permit.rs`)

### Summary
The `pallet-assets-precompiles` permit implementation reproduces the exact bug class from the external report: the EIP‑712 domain separator binds a **fixed** chain identifier (`T::ChainId: Get<u64>`) that is a compile-time/runtime config constant, not a value derived from the chain's genesis hash or any other chain-unique fingerprint [1](#0-0) . Because `compute_domain_separator` only mixes in `T::ChainId::get()` and the `verifying_contract` address [2](#0-1) , any two independently-deployed Substrate chains that reuse the same numeric `ChainId` (a very plausible scenario for testnet clones, canary/staging forks, or copy-pasted runtime configs) and assign the same asset to the same deterministic precompile address will produce byte-identical domain separators and permit digests. A signature an owner produces to authorize a `permit()` on one chain is therefore valid and replayable on the other.

### Finding Description
Substrate's native extrinsic replay protection uses `CheckGenesis`, which binds signed payloads to the chain's actual genesis block hash (`Pallet::<T>::block_hash(0)`) [3](#0-2) , precisely because chain forks/clones/testnets can share spec version but never share genesis hash. The new permit precompile bypasses this pattern entirely: it hard-codes replay-domain via a `#[pallet::constant] type ChainId: Get<u64>` supplied at runtime-config time [4](#0-3) , and uses it, together with only the `verifying_contract` (precompile) address and token name, to build the domain separator that gates every `permit()` signature [5](#0-4) , [6](#0-5) . There is no genesis hash, spec version, or any other network-fingerprint component in the digest.

The existing tests confirm the domain separator only varies with `verifying_contract` and token `name` [7](#0-6) , and that changing token metadata invalidates permits [8](#0-7) , but no test or code path recomputes/binds the separator to anything chain-unique like a genesis hash — mirroring exactly the "chain ID fixed at deployment, no fork detection" flaw described in the external ERC20Permit report.

### Impact Explanation
If two Substrate chains (e.g., a mainnet parachain and its testnet/staging clone, or a chain that is hard-forked/re-launched while keeping the same runtime config) configure the same `ChainId` constant — which requires no special coordination since it is just an arbitrary `u64` chosen by the runtime developer and commonly reused across environments — and assign the same asset index (thus the same deterministic precompile `verifying_contract` address), a permit signed by a token owner on one chain becomes a valid, unconsumed signature on the other chain. An attacker holding that signature (e.g., a spender who received a permit on the test chain) can replay it on the production chain to obtain an approval and drain the owner's allowance there, since `use_permit`'s nonce state is chain-local and starts fresh on each independent chain [9](#0-8) . This is unauthorized allowance creation/theft of asset value without any privileged actor, satisfying the "theft or unbacked... unauthorized execution" impact class.

### Likelihood Explanation
The likelihood is tied to operational practice rather than attacker sophistication: any deployment (testnet, canary, disaster-recovery fork, or accidental redeploy) that reuses the same `ChainId` config value — the most common case, since chain operators frequently copy runtime configuration between environments and there is no builtin uniqueness guarantee or warning for this constant — creates the collision. No malicious peer, validator, or governance action is required; the bug is purely in the domain-separator construction not incorporating a chain-unique value such as `frame_system::Pallet::<T>::block_hash(Zero::zero())`, unlike every other replay-protection extension in the codebase (`CheckGenesis`) [10](#0-9) .

### Recommendation
Include the chain's genesis hash (`frame_system::Pallet::<T>::block_hash(BlockNumberFor::<T>::zero())`) or another verifiably chain-unique value in `compute_domain_separator` in addition to (or instead of) the static `T::ChainId` constant, consistent with how `CheckGenesis` protects native extrinsics. At minimum, document and enforce that `ChainId` must be globally unique per deployed chain instance, and consider caching+recomputing the separator if the bound value ever changes, per the short-term mitigation in the original report.

### Proof of Concept
1. Deploy runtime A with `impl permit::Config for RuntimeA { type ChainId = ConstU64<1284>; ... }` and asset index `N`, giving precompile address `verifying_contract`.
2. Deploy an independent runtime B (e.g., a testnet clone) with the same `type ChainId = ConstU64<1284>` and the same asset index `N`, yielding the identical `verifying_contract`.
3. Owner signs a permit digest per `Pallet::<T>::permit_digest(verifying_contract, name, owner, spender, value, nonce=0, deadline)` [11](#0-10)  intending it for use on chain A only.
4. Because `name`, `verifying_contract`, and `ChainId` are identical on both chains, and both chains' nonce for `(verifying_contract, owner)` starts at `0`, the exact same `(v, r, s)` recovers to `owner` and is accepted by `do_verify_permit`/`use_permit` on chain B as well [12](#0-11) , granting the spender an allowance on chain B that the owner never authorized there.

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

**File:** substrate/frame/system/src/extensions/check_genesis.rs (L27-34)
```rust
/// Genesis hash check to provide replay protection between different networks.
///
/// # Transaction Validity
///
/// Note that while a transaction with invalid `genesis_hash` will fail to be decoded,
/// the extension does not affect any other fields of `TransactionValidity` directly.
#[derive(Encode, Decode, DecodeWithMemTracking, Clone, Eq, PartialEq, TypeInfo)]
#[scale_info(skip_type_params(T))]
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

**File:** substrate/frame/assets/precompiles/src/permit_tests.rs (L196-224)
```rust
#[test]
fn domain_separators_differ_per_verifying_contract() {
	new_test_ext().execute_with(|| {
		let contract_1 = H160::from_low_u64_be(0x1111);
		let contract_2 = H160::from_low_u64_be(0x2222);
		let name = test_token_name();

		let separator1 = permit::Pallet::<Test>::compute_domain_separator(&contract_1, name);
		let separator2 = permit::Pallet::<Test>::compute_domain_separator(&contract_2, name);

		// Domain separators should be different for different verifying contracts
		assert_ne!(separator1, separator2);
	});
}

#[test]
fn domain_separators_differ_per_token_name() {
	new_test_ext().execute_with(|| {
		let verifying_contract = test_verifying_contract();

		let separator1 =
			permit::Pallet::<Test>::compute_domain_separator(&verifying_contract, b"Token A");
		let separator2 =
			permit::Pallet::<Test>::compute_domain_separator(&verifying_contract, b"Token B");

		// Domain separators should be different for different token names
		assert_ne!(separator1, separator2);
	});
}
```

**File:** substrate/frame/assets/precompiles/src/permit_precompile_tests.rs (L887-926)
```rust
/// Renaming an asset invalidates outstanding permits — the EIP-712
/// domain separator binds the asset's current `name` metadata. Kept
/// parametrized over both prefixes for confidence on this
/// security-relevant invariant.
#[test_case(PRECOMPILE_ADDRESS_PREFIX)]
#[test_case(PRECOMPILE_ADDRESS_PREFIX_FOREIGN)]
fn permit_rejects_after_token_name_change(asset_index: u16) {
	new_test_ext().execute_with(|| {
		let setup = permit_setup(asset_index);

		let (v, r, s) =
			sign_permit(setup.asset_addr, setup.spender_addr, AlloyU256::from(100), setup.deadline);

		assert_ok!(Assets::force_set_metadata(
			RuntimeOrigin::root(),
			setup.asset_id,
			b"Renamed Token".to_vec(),
			b"RNM".to_vec(),
			18,
			false,
		));

		let result = raw_permit(
			setup.submitter,
			setup.asset_addr,
			HARDHAT_ACCOUNT_0,
			setup.spender_addr,
			AlloyU256::from(100),
			setup.deadline,
			v,
			r,
			s,
		);
		assert_permit_reverted_with(result, "Signer does not match owner");
		assert_eq!(
			permit::Pallet::<Test>::nonce(&setup.asset_addr, &HARDHAT_ACCOUNT_0),
			U256::zero()
		);
	});
}
```
