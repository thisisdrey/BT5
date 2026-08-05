## Title
Cross-chain replay of `pallet-nfts` pre-signed mint/attribute approvals due to missing chain-domain binding - (File: `substrate/frame/nfts/src/lib.rs`)

### Summary
`pallet_nfts::mint_pre_signed` and `pallet_nfts::set_attributes_pre_signed` authorize privileged, value-bearing actions (minting an item, setting collection-owner attributes) from an off-chain signature that never binds the signature to a specific chain/genesis. This is the same broken invariant as the reported `KinoAccountMinterUpgradable.mintBySignature` bug: a signature meant for use on one deployment can be captured and replayed on a different deployment that shares the same `pallet_nfts` runtime, the same `AccountId32` signer, and the same `collection`/`item` identifiers.

### Finding Description
`mint_pre_signed` verifies the signature over the raw SCALE encoding of `PreSignedMint` and then executes the mint: [1](#0-0) 

`set_attributes_pre_signed` follows the identical pattern: [2](#0-1) 

Both call into `Pallet::validate_signature`, which only checks that the bytes were signed by `signer` — nothing about which chain, genesis hash, spec version, or "verifying contract" (i.e., pallet/collection deployment) the signature is scoped to is included in the signed payload: [3](#0-2) 

The signed struct itself (`PreSignedMint`) contains only `collection`, `item`, `attributes`, `metadata`, `only_account`, `deadline`, `mint_price` — no genesis hash, chain ID, or other domain separator: [4](#0-3) 

`deadline` is a **block number**, not a wall-clock timestamp, and replay protection against *reuse on the same chain* is achieved only by `Item`/`Attribute` storage already existing (`AlreadyExists`/idempotent attribute overwrite) — there is no chain-scoping at all. Compare this to `frame_system`'s transaction extensions, where `CheckGenesis` explicitly documents that genesis-hash binding exists "to provide replay protection between different networks": [5](#0-4) 

`pallet_nfts`'s pre-signed mint/attribute flow bypasses this protection entirely because it is a pallet-level signature scheme, not a `TransactionExtension`, and was never given an equivalent domain separator.

`pallet_nfts` (or its `Instance` variants) is deployed on multiple independent chains that can share identical genesis-agnostic state shapes: e.g. Polkadot AssetHub, Kusama AssetHub, Westend AssetHub, and various other parachains/instances all run the same pallet code with the same `AccountId32` keyspace. Because `signer` is checked purely as `T::AccountId` (an `sr25519`/`ed25519`/`ecdsa` public key derived account with no chain prefix baked into the check), the same key pair produces the same `AccountId` on every chain. If the same collection id / item id layout exists (or can be caused to exist, e.g., attacker creates a matching empty collection ahead of time) on a second chain where the signer also holds an Issuer/Admin role, the exact same `(mint_data, signature, signer)` triple extracted from chain A's mempool/block can be resubmitted on chain B by any signed account, since `mint_pre_signed`/`set_attributes_pre_signed` accept `origin = ensure_signed(origin)?` from **any** account, not just the original submitter.

### Impact Explanation
An Issuer/Admin who pre-signs a mint or attribute-set approval intending it for use on one chain (e.g., a testnet or a specific parachain deployment) has that authorization silently valid on any other chain/instance running the same pallet, as long as an attacker can align `collection`/`item` state and the deadline block-number window. This can cause unauthorized minting of NFTs (with knock-on effects for `mint_price` currency transfers) and unauthorized attribute changes/deposits on a chain the signer never intended to authorize, violating the "settle exactly once to the rightful beneficiary" and "no forged/mis-bound proof acceptance" invariants in scope. It is a public-entrypoint issue: any signed account (not the original signer, not a privileged actor) can submit the replayed payload via the ordinary extrinsic.

### Likelihood Explanation
Requires: (1) the victim's signature bytes are observable (mempool/gossip/on-chain history — inherent to any pre-signed approval scheme), (2) a second chain instance running `pallet_nfts` where the same `AccountId` holds the necessary collection role and the same `collection`/`item` ids are free (attacker can often arrange this by creating the collection first), and (3) the `deadline` block-number window not yet passed on that second chain. This is realistic for multi-chain ecosystems (Polkadot/Kusama/Westend AssetHub, or any parachain template reusing `pallet_nfts`) where operators/marketplaces routinely reuse the same signer key across environments, exactly mirroring the report's "Alice signs on chain A, Bob replays on chain B" scenario. No malicious validator/collator/relayer/admin is needed — it is exploitable by an ordinary user.

### Recommendation
Add an explicit domain separator to the signed payload for `mint_pre_signed`/`set_attributes_pre_signed`, e.g. include `frame_system::Pallet::<T>::block_hash(Zero::zero())` (genesis hash) and/or `T::Version::get().spec_version` in `PreSignedMint`/`PreSignedAttributes` before encoding for signing, analogous to `frame_system::CheckGenesis`/`CheckSpecVersion`. Alternatively, mix in a pallet-specific and chain-specific context string plus the genesis hash into the hashed message passed to `validate_signature`, and document that signers must re-sign per-chain. This mirrors the EIP-712 domain-separator fix recommended in the source report.

### Proof of Concept
1. On Chain A, an Issuer signs `PreSignedMint { collection: 0, item: 0, attributes: [...], metadata: [...], only_account: None, deadline: D, mint_price: Some(P) }` and gives the `(mint_data, signature, signer)` triple to a marketplace bot, which submits `mint_pre_signed` on Chain A — item 0 in collection 0 is minted there. [6](#0-5) 
2. An observer extracts `(mint_data, signature, signer)` from Chain A's block/mempool.
3. On Chain B (a different parachain/network instance running the same `pallet_nfts`), the same `signer` `AccountId` already has (or the attacker arranges for the signer to have, e.g. by inviting them to create a matching collection) Issuer rights on a collection with id `0`, and item `0` is still free there, with the current block number on Chain B still `< D`.
4. Any account on Chain B calls `Nfts::mint_pre_signed(origin, mint_data, signature, signer)` with the exact bytes from Chain A. `validate_signature` succeeds because it only checks `signature.verify(&data, &signer)` with no chain-specific data in `data`: [7](#0-6) 
5. The mint (and any `mint_price` currency debit from the *caller* on Chain B, not Chain A) succeeds, even though the Issuer never intended to authorize a mint on Chain B.

**Uncertainty note:** I could not fully confirm from the index whether any currently deployed production runtime configures `pallet_nfts` such that the same signer key and matching `collection`/`item` ids realistically co-exist across two live chains today (this depends on deployment-specific collection numbering, which requires live-chain state inspection outside this repo's index). The vulnerability described is a structural gap in the pallet's signature scheme (no domain separator) rather than a confirmed exploited instance; a Devin session with access to live chain state/history would be needed to fully validate real-world exploitability across specific deployed chains.

### Citations

**File:** substrate/frame/nfts/src/lib.rs (L1897-1908)
```rust
		#[pallet::call_index(37)]
		#[pallet::weight(T::WeightInfo::mint_pre_signed(mint_data.attributes.len() as u32))]
		pub fn mint_pre_signed(
			origin: OriginFor<T>,
			mint_data: Box<PreSignedMintOf<T, I>>,
			signature: T::OffchainSignature,
			signer: T::AccountId,
		) -> DispatchResult {
			let origin = ensure_signed(origin)?;
			Self::validate_signature(&Encode::encode(&mint_data), &signature, &signer)?;
			Self::do_mint_pre_signed(origin, *mint_data, signer)
		}
```

**File:** substrate/frame/nfts/src/lib.rs (L1923-1934)
```rust
		#[pallet::call_index(38)]
		#[pallet::weight(T::WeightInfo::set_attributes_pre_signed(data.attributes.len() as u32))]
		pub fn set_attributes_pre_signed(
			origin: OriginFor<T>,
			data: PreSignedAttributesOf<T, I>,
			signature: T::OffchainSignature,
			signer: T::AccountId,
		) -> DispatchResult {
			let origin = ensure_signed(origin)?;
			Self::validate_signature(&Encode::encode(&data), &signature, &signer)?;
			Self::do_set_attributes_pre_signed(origin, data, signer)
		}
```

**File:** substrate/frame/nfts/src/common_functions.rs (L41-62)
```rust
	pub fn validate_signature(
		data: &Vec<u8>,
		signature: &T::OffchainSignature,
		signer: &T::AccountId,
	) -> DispatchResult {
		if signature.verify(&**data, &signer) {
			return Ok(());
		}

		// NOTE: for security reasons modern UIs implicitly wrap the data requested to sign into
		// <Bytes></Bytes>, that's why we support both wrapped and raw versions.
		let prefix = b"<Bytes>";
		let suffix = b"</Bytes>";
		let mut wrapped: Vec<u8> = Vec::with_capacity(data.len() + prefix.len() + suffix.len());
		wrapped.extend(prefix);
		wrapped.extend(data);
		wrapped.extend(suffix);

		ensure!(signature.verify(&*wrapped, &signer), Error::<T, I>::WrongSignature);

		Ok(())
	}
```

**File:** substrate/frame/nfts/src/tests.rs (L3297-3305)
```rust
		let mint_data: PreSignedMint<u32, u32, AccountId, u32, u64> = PreSignedMint {
			collection: 0,
			item: 0,
			attributes: vec![],
			metadata: vec![],
			only_account: None,
			deadline: 100000,
			mint_price: None,
		};
```

**File:** substrate/frame/nfts/src/tests.rs (L3327-3354)
```rust
		let mint_data = PreSignedMint {
			collection: 0,
			item: 0,
			attributes: vec![(vec![0], vec![1]), (vec![2], vec![3])],
			metadata: vec![0, 1],
			only_account: None,
			deadline: 10000000,
			mint_price: Some(10),
		};
		let message = Encode::encode(&mint_data);
		let signature = MultiSignature::Sr25519(user_1_pair.sign(&message));
		let user_2 = account(2);
		let user_3 = account(3);

		Balances::make_free_balance_be(&user_0, 100);
		Balances::make_free_balance_be(&user_2, 100);
		assert_ok!(Nfts::create(
			RuntimeOrigin::signed(user_0.clone()),
			user_1.clone(),
			collection_config_with_all_settings_enabled(),
		));

		assert_ok!(Nfts::mint_pre_signed(
			RuntimeOrigin::signed(user_2.clone()),
			Box::new(mint_data.clone()),
			signature.clone(),
			user_1.clone(),
		));
```

**File:** substrate/frame/system/src/extensions/check_genesis.rs (L27-35)
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
```
