Based on the investigation, I found a genuine local analog to the "missing initializer call" bug class in the EIP-2612 `permit()` implementation added to `pallet-assets-precompiles`.

### Title
`permit()` computes the EIP-712 domain separator from an asset's name without requiring that name/metadata was ever initialized, unlike the equivalent `name()` view - (File: `substrate/frame/assets/precompiles/src/lib.rs`)

### Summary
The Solidity report flags that `BasketToken.initialize()` omits `__ERC20Permit_init()`, so the EIP-712 name component that binds permit signatures to the token's identity is never properly set up. The polkadot-sdk repository re-implements the same EIP-2612 permit mechanism natively in `pallet-assets-precompiles` (added in `prdoc/stable2603/pr_11044.prdoc`), and it has the analogous gap: the code path that feeds the "name" component into the domain separator for `permit()` does not enforce that the asset's metadata (name) has actually been initialized, while the sibling `name()` getter explicitly does enforce it.

### Finding Description
In `ERC20::permit`, the pallet fetches the token name directly from storage and feeds it straight into the domain-separator computation: [1](#0-0) 

Compare this to the `name()` view function of the same precompile, which explicitly requires metadata to exist and reverts otherwise: [2](#0-1) 

and `domain_separator()`, which likewise fetches the raw name with no existence check before binding it into the EIP-712 domain: [3](#0-2) 

The actual domain-separator construction is in the `permit` pallet: [4](#0-3) 

The doc comments for the pallet explicitly acknowledge that `force_set_metadata` changing the name changes the domain separator and invalidates outstanding permits — this is documented as intended behavior, confirming the name is treated as a live, mutable, security-critical binding component, exactly like `__ERC20Permit_init(name)` caches `_EIP712NameHash` in the OpenZeppelin analog. However, unlike `name()`, the `permit()`/`DOMAIN_SEPARATOR()` code paths never verify that this critical initialization value (`Metadata::<T,I>` for the asset) was ever set at all before using it to build/verify signatures.

### Impact Explanation
For any asset created via `pallet_assets::Pallet::force_create`/`create` before an explicit `set_metadata`/`force_set_metadata` call, `permit()` silently accepts signatures computed against an "uninitialized" domain separator (empty-name component) rather than rejecting or requiring that the token's identity be initialized first — mirroring the report's core issue: a critical initialization step for a permit/signature feature is silently skipped instead of being enforced, and the feature is still exposed to users as if it were correctly configured. Once metadata is later set, the domain separator changes and every previously issued/expected permit becomes invalid without any explicit signal to integrators that the earlier permits were built against an uninitialized identity.

### Likelihood Explanation
Likelihood is high in practice, since asset creation and metadata-setting are commonly separate transactions/extrinsics (`force_create` vs `set_metadata`), and nothing in the precompile dispatcher prevents `permit()`/`DOMAIN_SEPARATOR()` from being called against an asset in this intermediate, metadata-less state.

### Recommendation
Mirror the `name()` getter's behavior in the `permit()` and `domain_separator()` code paths: require `pallet_assets::Pallet::get_metadata(asset_id)` to return `Some(_)` before allowing permit signature verification/consumption, reverting (as `name()` already does) if metadata has not been initialized for the asset. This ensures the EIP-712 domain-separator name component can never be computed against an uninitialized identity.

### Proof of Concept
1. Call `pallet_assets::force_create` for a new `asset_id` without calling `set_metadata`/`force_set_metadata`.
2. Compute/sign an EIP-712 permit off-chain using an empty string for `name` (matching the pallet's default unset metadata).
3. Submit `permit()` via the precompile dispatcher (as exercised in `permit_precompile_tests.rs`); it succeeds and updates the allowance, even though the token's identity (`name`) was never initialized. Later calling `force_set_metadata` to actually name the token silently invalidates the previously granted approval logic path, evidencing that the pre-metadata state was accepted without any initialization guard. [5](#0-4)

### Citations

**File:** substrate/frame/assets/precompiles/src/lib.rs (L503-516)
```rust
		let transaction_outcome = frame_support::storage::with_transaction(|| {
			let result = (|| {
				// Use the permit - this validates deadline, signature, and increments nonce
				permit::Pallet::<Runtime>::use_permit(
					&verifying_contract,
					&pallet_assets::Pallet::<Runtime, Instance>::name(asset_id.clone()),
					&owner_h160,
					&spender_h160,
					&value_bytes,
					&deadline_bytes,
					call.v,
					&r_bytes,
					&s_bytes,
				)
```

**File:** substrate/frame/assets/precompiles/src/lib.rs (L645-661)
```rust
	/// Get the EIP-712 domain separator for this contract.
	fn domain_separator(
		asset_id: <Runtime as Config<Instance>>::AssetId,
		verifying_contract: H160,
		env: &mut impl Ext<T = Runtime>,
	) -> Result<Vec<u8>, Error> {
		env.charge(<Runtime as permit::Config>::WeightInfo::domain_separator())?;

		// Fetch token name for EIP-712 domain separator (per EIP-2612 spec)
		let token_name = pallet_assets::Pallet::<Runtime, Instance>::name(asset_id);

		let separator =
			permit::Pallet::<Runtime>::compute_domain_separator(&verifying_contract, &token_name);
		let separator_alloy: alloy::primitives::FixedBytes<32> = separator.0.into();

		Ok(IERC20::DOMAIN_SEPARATORCall::abi_encode_returns(&separator_alloy))
	}
```

**File:** substrate/frame/assets/precompiles/src/lib.rs (L663-677)
```rust
	/// Execute the name call.
	fn name(
		asset_id: <Runtime as Config<Instance>>::AssetId,
		env: &mut impl Ext<T = Runtime>,
	) -> Result<Vec<u8>, Error> {
		env.charge(<Runtime as Config<Instance>>::WeightInfo::get_metadata())?;

		let metadata = pallet_assets::Pallet::<Runtime, Instance>::get_metadata(asset_id)
			.ok_or(Error::Revert(Revert { reason: "Metadata not found".into() }))?;

		let name = alloc::string::String::from_utf8(metadata.name.to_vec())
			.map_err(|_| Error::Revert(Revert { reason: "Invalid UTF-8 in name".into() }))?;

		Ok(IERC20::nameCall::abi_encode_returns(&name))
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

**File:** substrate/frame/assets/precompiles/src/permit_precompile_tests.rs (L132-152)
```rust
/// Configures an asset owned by Hardhat #0 with metadata name
/// [`PERMIT_TOKEN_NAME`], returning the asset's precompile address.
/// Hardhat #0 is set as the asset admin so freeze tests can drive
/// `freeze_asset` from that account.
fn setup_permit_asset(asset_id: u32, prefix: u16) -> H160 {
	let asset_addr = H160::from(set_prefix_in_address(prefix));
	let owner = hardhat_account_id();
	Balances::make_free_balance_be(&owner, 1_000);
	setup_asset_for_prefix(asset_id, prefix);
	assert_ok!(Assets::force_create(RuntimeOrigin::root(), asset_id, owner, true, 1));
	assert_ok!(Assets::force_set_metadata(
		RuntimeOrigin::root(),
		asset_id,
		PERMIT_TOKEN_NAME.to_vec(),
		b"TST".to_vec(),
		18,
		false,
	));
	assert_ok!(Assets::mint(RuntimeOrigin::signed(owner), asset_id, owner, 100));
	asset_addr
}
```
