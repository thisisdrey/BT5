### Title
`pallet_identity::set_username_for` signature has no chain/instance binding, enabling cross-chain signature replay — ([File: substrate/frame/identity/src/lib.rs])

### Summary
`pallet_identity::Pallet::set_username_for` lets a registered username authority grant a username to a user by presenting a signature the user produced over the *raw username bytes* only. That signed payload contains no genesis hash, chain ID, or pallet-instance identifier, so a signature a user created to authorize a username grant on one chain (e.g. `people-westend`) can be replayed verbatim by any authority on another chain instance (e.g. `people-kusama`, or any other deployment of `pallet-identity` sharing the same suffix and where the user's `AccountId` derives from the same public key). This mirrors the AffiliateValidator flaw: the signed data lacks the "verifying contract"-equivalent binding, so a signature meant for one instance is valid on every instance.

### Finding Description
`set_username_for` accepts an optional `T::OffchainSignature` and validates it with `Self::validate_signature(&bounded_username[..], &s, &who)`: [1](#0-0) 

The signed message is exactly the username+suffix bytes (optionally wrapped in `<Bytes>…</Bytes>` for wallet display), as shown by the test helpers signing `&bounded_username[..]` / `&username[..]`: [2](#0-1) [3](#0-2) 

Nothing in that payload — no genesis hash, no `SS58Prefix`/chain ID, no specific authority `AccountId`, no pallet instance discriminator — ties the signature to the chain or authority the user intended it for. `pallet-identity` (and this exact `set_username_for` mechanism) is deployed identically on multiple chains that use compatible `AccountId` derivation (e.g. sr25519/ed25519 `AccountId32`), such as `people-westend`, `people-kusama`, and other system/parachains. Because the user's `AccountId` is derived purely from their public key and is independent of genesis hash, the *same* `AccountId` and the *same* signature bytes are valid across all these deployments.

Contrast this with the EIP-712 based `permit` pallet in this same repository, which explicitly folds the `verifying_contract` address and `chainId` into the domain separator specifically to prevent this class of bug: [4](#0-3) 

`pallet_identity::set_username_for` has no equivalent binding, so the guard that stops the AffiliateValidator-class bug elsewhere in the codebase is simply absent here.

### Impact Explanation
Any account already registered as a username authority for suffix `S` on chain B (a normal, ungoverned action reachable by any interested party who is granted a suffix — this is not a super-admin capability, and multiple independent authorities/suffixes coexist by design) can take a username-authorization signature that a user produced for chain A (same suffix `S`, same authority-relationship pattern) and submit it via `set_username_for` on chain B. This lets the authority on chain B unilaterally bind a username to the victim's account there — without the victim ever intending or consenting to that specific chain/authority pairing — consuming the authority's own allocation/deposit and creating an unwanted, difficult-to-remove (`unbind_username`/`kill_username` gated to the authority/root) identity binding for the victim account on a chain they never interacted with. This is unauthorized state mutation stemming from a mis-bound signature acceptance, matching the "forged or mis-bound proof/state acceptance" impact class.

### Likelihood Explanation
Exploitation requires only (a) knowledge of a username-signature the victim produced for one chain and (b) control of any authority slot with a matching suffix on a different chain/instance — both realistic since suffixes are cheap to register and signatures are often shared publicly/off-chain (e.g. posted to claim a username) to be countersigned by an authority. No validator, relayer, or leaked-key assumption is required; the "attacker" is any ordinary username authority, a role explicitly reachable by ordinary participants under the pallet's design.

### Recommendation
Include chain- and instance-binding data in the signed payload for `set_username_for`, e.g. incorporate `frame_system::Pallet::<T>::block_hash(0)` (genesis hash) and/or a pallet-instance/authority-account identifier into the message that is hashed and signed, mirroring the `permit` pallet's inclusion of `chainId` and `verifyingContract` in its EIP-712 domain separator.

### Proof of Concept
1. On chain A, a user signs `username_to_sign = b"alice.kyc"` off-chain intending to authorize Authority A1 (suffix `kyc` on chain A) to call `set_username_for`.
2. The attacker, who controls Authority A2 registered for the same suffix `kyc` on chain B (a different `pallet-identity` deployment sharing `AccountId32` derivation), submits the identical signature bytes via `set_username_for(origin = A2, who = alice's AccountId, username = b"alice.kyc", signature = Some(sig), ..)` on chain B.
3. `Identity::validate_signature(&bounded_username[..], &s, &who)` succeeds because the signed bytes (`b"alice.kyc"`) and signer (`alice`) match exactly — no chain-specific data was ever included — and the username is bound to Alice's account on chain B without her consent for that chain, per: [5](#0-4)

### Citations

**File:** substrate/frame/identity/src/lib.rs (L1240-1251)
```rust
			// Insert or queue.
			let who = T::Lookup::lookup(who)?;
			if let Some(s) = signature {
				// Account has pre-signed an authorization. Verify the signature provided and grant
				// the username directly.
				Self::validate_signature(&bounded_username[..], &s, &who)?;
				Self::insert_username(&who, bounded_username, provider);
			} else {
				// The user must accept the username, therefore, queue it.
				Self::queue_acceptance(&who, bounded_username, provider);
			}
			Ok(())
```

**File:** substrate/frame/identity/src/benchmarking.rs (L636-644)
```rust
		let username = bench_username();
		let bounded_username = bounded_username::<T>(username.clone(), suffix.clone());

		let (public, signature) = T::BenchmarkHelper::sign_message(&bounded_username[..]);
		let who_account = public.into_account();
		let who_lookup = T::Lookup::unlookup(who_account.clone());

		// Verify signature here to avoid surprise errors at runtime
		assert!(signature.verify(&bounded_username[..], &who_account));
```

**File:** substrate/frame/identity/src/tests.rs (L1181-1192)
```rust
		let public = sr25519_generate(0.into(), None);
		let who_account: AccountIdOf<Test> = MultiSigner::Sr25519(public).into_account().into();
		let signature =
			MultiSignature::Sr25519(sr25519_sign(0.into(), &public, &username[..]).unwrap());

		assert_ok!(Identity::set_username_for(
			RuntimeOrigin::signed(authority.clone()),
			who_account.clone(),
			username.clone().into(),
			Some(signature),
			true,
		));
```

**File:** substrate/frame/assets/precompiles/src/permit.rs (L148-176)
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

```
