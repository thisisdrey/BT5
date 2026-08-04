Based on my investigation, `pallet-assets::create` is permissionless via `T::CreateOrigin` (any signed account can create an asset with an arbitrary `id`, subject to `AssetIdAllocator`), and `asset_id_cannot_be_reused` in `substrate/frame/assets/src/tests.rs` confirms that when the id allocator does not enforce a strict sequence, a previously-destroyed asset `id` can be freely recreated by anyone. This matches the report's core invariant: an externally verified identity ("verifying_contract"/precompile address, analogous to Atlas's "DAppControl address") stays fixed while the underlying configuration it represents can be swapped out from under it. The EIP-2612 `permit` implementation in `substrate/frame/assets/precompiles/src/permit.rs` binds its domain separator only to `(chainId, verifyingContract, name)` and the nonce store `Nonces<T>` is keyed only by `(verifying_contract, owner)` — neither is tied to an asset "epoch"/generation, so a destroy+recreate cycle that reuses the same `id` (hence same precompile address) and coincidentally/deliberately reuses the same token `name` produces an *identical* domain separator and *unchanged* stored nonce, making a previously signed-but-unused `permit()` from the original asset silently valid against the new, unrelated asset instance.

### Title
Stale EIP-2612 `permit` signatures remain valid across asset destroy/recreate because domain separator and nonce are not bound to asset identity - (File: `substrate/frame/assets/precompiles/src/permit.rs`)

### Summary
The EIP-2612 `permit` precompile computes its EIP-712 domain separator from `(chainId, verifyingContract, name)` [1](#0-0)  and tracks replay protection in `Nonces<T>`, keyed only by `(verifying_contract, owner)` [2](#0-1) . Because `pallet-assets` allows a destroyed asset `id` to be recreated by any signed account under `T::CreateOrigin` when no strict allocator sequence is enforced [3](#0-2) , and the precompile address is deterministically derived from that same `id` [4](#0-3) , neither the domain separator nor the nonce changes when the asset's true identity (owner, supply, decimals, holders) is fully replaced.

### Finding Description
`compute_domain_separator` only incorporates `chainId`, `verifyingContract` and the current on-chain `name` metadata string; it has no concept of an asset "generation" or creation nonce [1](#0-0) . The test `permit_rejects_after_token_name_change` confirms the developers only defend against a *name* change, not against a full asset destroy-and-recreate at the same `id`/address [5](#0-4) . Separately, `asset_id_cannot_be_reused` shows that reuse of a destroyed asset `id` is explicitly permitted "till auto increment is not enabled" [6](#0-5) , meaning `create` with an explicit `id` is a public entrypoint that lets any user recreate an asset at a previously-used address as soon as it is fully destroyed. `Nonces<T>` for the permit pallet is never cleared on asset destruction — it lives in an entirely separate pallet (`pallet_assets_precompiles::permit`) with no hook into `pallet_assets`'s destroy lifecycle [2](#0-1) . Consequently, if an attacker destroys asset `id=X` and recreates it with the identical `name` (fully within their control since they are the new asset's creator/admin), an old permit signature that a victim previously produced for the original asset `X`, spender `S`, at the current nonce, but never submitted on-chain, becomes a valid approval on the brand-new, attacker-controlled asset instance — because `verifying_contract`, `name`, and `nonce` all match exactly, so `permit_digest` is byte-identical [7](#0-6) .

### Impact Explanation
This lets an attacker who controls asset destroy+recreate (permissionless for the specific `id`) obtain an ERC-20 `approve`-equivalent allowance on an asset the victim never intended to interact with, using a signature the victim generated for a completely different, prior token. If the victim's wallet software (relying on the deterministic domain separator/verifying-contract binding for safety) had granted or cached a not-yet-submitted permit, the attacker can weaponize `permit()` to move funds the victim holds in the *new* asset (e.g., if the victim also holds the recreated asset by virtue of the address collision, or if the spender then calls `transferFrom`). This is a false state acceptance of a stale authorization due to a hash/domain-separator collision across mutable on-chain configuration, directly analogous to the reported issue where a DAppControl's mutable `CallConfig` was not bound into signed struct hashes.

### Likelihood Explanation
Exploitability requires the attacker to fully destroy and recreate the specific asset `id` with a matching `name` — feasible since asset `create`/`destroy` on this `id` may be under attacker control if the attacker is (or becomes) the current asset owner, or if the `id` had already been destroyed and is free for anyone to claim (`asset_id_cannot_be_reused` demonstrates unrestricted reuse is possible outside of allocator-sequenced ids). It further requires a victim to have produced (signed) but not yet submitted a permit for a nonce that remains current after the swap. This narrows likelihood versus a fully automatic exploit, but it is a real, unprivileged, non-governance attack path that violates the intended domain-separation guarantee of EIP-712/EIP-2612.

### Recommendation
Bind the permit domain separator (or nonce namespace) to an immutable, monotonically-changing identifier of the specific asset instance rather than only its mutable `name` and reusable `id`/address — e.g., include the asset's creation block number, a per-asset creation nonce/"generation" counter incremented on `create`, or the `AssetDetails.owner` at time of signing. Alternatively, reset/purge `Nonces<T>` entries for a `verifying_contract` whenever the underlying asset at that address is destroyed, via a `CallbackHandle`/`AssetsCallback` hook analogous to the one used for `foreign_assets` index cleanup [8](#0-7) .

### Proof of Concept
1. Asset `id=7` is created by Alice (owner), named `"Token"`. Victim signs (but does not submit) a `permit(owner=Victim, spender=Attacker, value=V, deadline=D)` for the ERC-20 precompile at address derived from `id=7`, at `nonce=0`.
2. Alice (or attacker, if attacker is owner/ForceOrigin) calls `start_destroy` → `destroy_accounts` → `destroy_approvals` → `finish_destroy` on asset `id=7`, per `substrate/frame/assets/src/tests.rs::partial_destroy_should_work` flow.
3. Attacker calls `Assets::create(origin, id=7, admin=Attacker, min_balance=1)` (permitted since `id=7` is no longer in use, per `asset_id_cannot_be_reused`), then sets metadata `name="Token"` to exactly match the destroyed asset's name.
4. `permit::Nonces::<T>::get(verifying_contract, victim)` is still `0` (never touched by asset destroy), and `compute_domain_separator` recomputes the identical digest because `verifying_contract`, `name`, and `chainId` are unchanged.
5. Attacker submits the victim's previously-signed permit calldata against the recreated asset; `do_verify_permit`/`permit_digest` matches and the signature is accepted, granting Attacker an allowance on the new asset that the victim never intended to authorize [9](#0-8) .

### Citations

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

**File:** substrate/frame/assets/precompiles/src/permit.rs (L220-238)
```rust
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

**File:** substrate/frame/assets/src/lib.rs (L843-861)
```rust
		pub fn create(
			origin: OriginFor<T>,
			id: T::AssetIdParameter,
			admin: AccountIdLookupOf<T>,
			min_balance: T::Balance,
		) -> DispatchResult {
			let id: T::AssetId = id.into();
			let owner = T::CreateOrigin::ensure_origin(origin, &id)?;
			let admin = T::Lookup::lookup(admin)?;

			ensure!(!Asset::<T, I>::contains_key(&id), Error::<T, I>::InUse);
			ensure!(!min_balance.is_zero(), Error::<T, I>::MinBalanceZero);

			if let Some(next_id) = T::AssetIdAllocator::next() {
				ensure!(id == next_id, Error::<T, I>::BadAssetId);
			}

			let deposit = T::AssetDeposit::get();
			T::Currency::reserve(&owner, deposit)?;
```

**File:** substrate/frame/assets/precompiles/src/lib.rs (L88-94)
```rust
impl AssetIdExtractor for InlineAssetIdExtractor {
	type AssetId = u32;
	fn asset_id_from_address(addr: &[u8; 20]) -> Result<Self::AssetId, Error> {
		let bytes: [u8; 4] = addr[0..4].try_into().expect("slice is 4 bytes; qed");
		let index = u32::from_be_bytes(bytes);
		Ok(index)
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

**File:** substrate/frame/assets/src/tests.rs (L2211-2231)
```rust
#[test]
fn asset_id_cannot_be_reused() {
	build_and_execute(|| {
		Balances::make_free_balance_be(&1, 100);
		// Asset id can be reused till auto increment is not enabled.
		assert_ok!(Assets::create(RuntimeOrigin::signed(1), 0, 1, 1));

		assert_ok!(Assets::start_destroy(RuntimeOrigin::signed(1), 0));
		assert_ok!(Assets::finish_destroy(RuntimeOrigin::signed(1), 0));

		assert!(!Asset::<Test>::contains_key(0));

		// Asset id `0` is reused.
		assert_ok!(Assets::create(RuntimeOrigin::signed(1), 0, 1, 1));
		assert!(Asset::<Test>::contains_key(0));

		assert_ok!(Assets::start_destroy(RuntimeOrigin::signed(1), 0));
		assert_ok!(Assets::finish_destroy(RuntimeOrigin::signed(1), 0));

		assert!(!Asset::<Test>::contains_key(0));

```

**File:** substrate/frame/assets/precompiles/src/foreign_assets.rs (L116-122)
```rust
		/// Remove an asset mapping if it exists, else this function has no effect.
		pub fn remove_asset_mapping(asset_id: &T::ForeignAssetId) {
			if let Some(asset_index) = ForeignAssetIdToAssetIndex::<T>::take(asset_id) {
				AssetIndexToForeignAssetId::<T>::remove(asset_index);
			}
		}
	}
```
