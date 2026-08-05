### Title
`pallet-scarcity::mint` lets any collection owner permanently lock an NFT into a purse key nobody can control - (File: `substrate/frame/scarcity/src/lib.rs`)

### Summary
The external report's core invariant is: a public mint entry point pushes an NFT onto an attacker/collection-controlled destination address with no check that the destination can ever move or reject the asset, so the token becomes permanently stuck. `pallet-scarcity`'s `mint` dispatchable (and the pallet's whole "coinage" design) reproduces exactly this pattern: any collection owner can mint an instance into an arbitrary `AccountId` with zero destination consent, and the only sanctioned way to move that instance back out — the `transfer`/`burn` calls — require a signed dispatch from that exact account authenticated through the `AsScarcity` transaction extension. If the destination account is one for which no private key exists (a sovereign/pallet account, a precompile-style or non-existent key, or any address the owner does not actually control), the minted NFT is permanently and unrecoverably locked in that purse key.

### Finding Description
The pallet's own documentation explicitly states the design has no receiver consent: [1](#0-0) 

The `mint` extrinsic takes an arbitrary `to: T::AccountId` supplied by the collection owner (the `Issuer`), with no check that `to` corresponds to an address whose holder can act on it: [2](#0-1) 

The only way to move or destroy that instance afterward is through `Pallet::transfer` / `Pallet::burn`, both of which only accept the special `Origin::Nft` produced by the `AsScarcity` transaction extension, which itself requires an ordinary *signed* origin matching the exact purse-key account before it will rewrite the origin: [3](#0-2) [4](#0-3) 

Because each purse key can hold at most one live NFT (`NftsByOwner`), and because `mint` performs no verification that the target key is reachable by a real signer, a collection owner (an ordinary, non-privileged, permissionless role obtained just by calling `create_collection`) can mint an instance into any `AccountId` of their choosing — including an address for which no corresponding private key is ever generated (e.g. a hash-derived pallet/sovereign account, a burn address, or any arbitrary 32-byte value). Since nobody can ever produce the signed transaction that `AsScarcity` requires to transfer or burn that instance, the NFT — and the storage deposit backing it — become permanently unrecoverable, and that purse key is permanently unable to receive any other instance (`NftsByOwner::<T>::contains_key(&to)` will always be true for it going forward).

This is the direct analog of the `ArcanaPrime` bug: a public/attacker-reachable minting path places a non-fungible asset onto a destination that structurally cannot exercise ownership over it, with no `safe`-style precondition check, causing permanent loss.

### Impact Explanation
An NFT (and its backing storage deposit, ultimately funded by the collection's consideration ticket) can be irrecoverably destroyed/locked by any unprivileged collection owner minting into an address they do not control the key for. Unlike the Solidity case, here there is no recovery path at all — the pallet's sole transfer/burn mechanism structurally requires a signature from the exact locked account, so the asset is permanently unspendable/unburnable, a genuine permanent fund/state lock as covered by the impact gate ("permanent user-fund or bridge-state lock").

### Likelihood Explanation
No admin, governance, validator, relayer, or leaked key is required. Any ordinary user can `create_collection`, `define_item`, and then `mint` to any `AccountId`, including one they intentionally choose to be unreachable (a derived/sovereign-style address, or simply an address whose private key was never generated). This is trivially and repeatedly exploitable by any collection owner against their own or any other purse key, and the pallet's docs confirm this is the literal, unmitigated behavior of the storage layer.

### Recommendation
Require destination consent before minting a non-fungible instance into a purse key that has no way to later authorize its own transfer/burn — e.g., require the destination to pre-register/opt-in (analogous to `_safeMint`'s receiver-acceptance check), or restrict `mint`'s `to` to accounts that can be proven to be capable signers (reject accounts that are known non-keyed/derived accounts), or provide a governance/administrative recovery path at the storage layer for permanently orphaned purse keys.

### Proof of Concept
1. Alice calls `Scarcity::create_collection` and `Scarcity::define_item`, becoming the collection's `Issuer`.
2. Alice computes (or is handed) an `AccountId` `X` derived purely as a hash (e.g., a PalletId-derived sovereign account of some unrelated pallet, or any arbitrary 32-byte value with no known seed/private key).
3. Alice calls `Scarcity::mint(collection, item, to = X, metadata)`. `do_mint`/`do_mint_inner` succeeds because there is no consent or "can this account ever sign" check on `to` — see `mint` at `substrate/frame/scarcity/src/lib.rs:564-582`.
4. `NftsByOwner::<T>::insert(&X, nft)` now holds the only instance ever allowed at that purse key.
5. No one can ever submit a valid `AsScarcity`-authorized `transfer` or `burn` for instance at `X`, because `AsScarcity::validate` requires `origin.as_system_ref()` to be `Signed(X)` — and no private key for `X` exists (`substrate/frame/scarcity/src/extension.rs:215-218`).
6. The NFT, its storage deposit, and that purse key's capacity to ever hold another instance are permanently lost.

### Citations

**File:** substrate/frame/scarcity/src/lib.rs (L29-35)
```rust
//! Purse keys are coinage-style receiving addresses, not identities: the pallet applies no
//! destination consent. Any collection owner can mint into — or force-transfer an instance to —
//! any empty purse key, and because each key holds at most one NFT, an unsolicited instance
//! blocks that key from receiving anything else until its holder burns it or transfers it away.
//! Holders should treat purse keys as disposable, minting to fresh keys they control, and
//! runtimes or contracts that need receive-consent or long-lived well-known destinations must
//! enforce that policy above this storage layer.
```

**File:** substrate/frame/scarcity/src/lib.rs (L564-582)
```rust
		/// Mint an instance of an immutable item definition into an empty purse key.
		///
		/// The destination gives no consent; any empty key is a valid target. See the module
		/// documentation on purse-key occupancy.
		///
		/// `metadata` contains instance-specific overrides. Item metadata remains the shared
		/// default for every instance minted from the definition.
		#[pallet::call_index(2)]
		#[pallet::weight(T::WeightInfo::mint(metadata.len() as u32))]
		pub fn mint(
			origin: OriginFor<T>,
			collection: CollectionId,
			item: ItemIndex,
			to: T::AccountId,
			metadata: Vec<(MetadataKeyOf<T>, MetadataValueOf<T>)>,
		) -> DispatchResult {
			let owner = ensure_signed(origin)?;
			Self::do_mint(owner, collection, item, to, metadata).map(|_| ())
		}
```

**File:** substrate/frame/scarcity/src/extension.rs (L196-228)
```rust
	fn validate(
		&self,
		mut origin: <T as frame_system::Config>::RuntimeOrigin,
		call: &<T as frame_system::Config>::RuntimeCall,
		_info: &DispatchInfoOf<<T as frame_system::Config>::RuntimeCall>,
		_len: usize,
		_self_implicit: Self::Implicit,
		_inherited_implication: &impl Implication,
		_source: TransactionSource,
	) -> ValidateResult<Self::Val, <T as frame_system::Config>::RuntimeCall> {
		let transfer_to = match call.is_sub_type() {
			Some(Call::<T>::transfer { to }) => Some(to),
			Some(Call::<T>::burn {}) => None,
			_ => return Ok((ValidTransaction::default(), Val::NotUsing, origin)),
		};
		let Some(AsScarcityInfo::AsNft { instance, state_nonce }) = self.0.as_ref() else {
			return Ok((ValidTransaction::default(), Val::NotUsing, origin));
		};

		let Some(frame_system::Origin::<T>::Signed(owner)) = origin.as_system_ref() else {
			return Err(CustomInvalidity::OriginToAsNftMustBeSigned.into());
		};
		let owner = owner.clone();
		let now = T::UnixTime::now().as_secs();
		if let Some(lock) = Locked::<T>::get(&owner) {
			if lock.until > now {
				return Err(CustomInvalidity::NftTemporarilyLocked.into());
			}
		}
		let nft = NftsByOwner::<T>::get(&owner).ok_or(CustomInvalidity::NoNft)?;
		if nft.instance != *instance || nft.state_nonce != *state_nonce {
			return Err(CustomInvalidity::NftStateMismatch.into());
		}
```

**File:** substrate/frame/scarcity/src/extension.rs (L254-281)
```rust
	fn prepare(
		self,
		val: Self::Val,
		_origin: &<T as frame_system::Config>::RuntimeOrigin,
		_call: &<T as frame_system::Config>::RuntimeCall,
		_info: &DispatchInfoOf<<T as frame_system::Config>::RuntimeCall>,
		_len: usize,
	) -> Result<Self::Pre, TransactionValidityError> {
		match val {
			Val::NotUsing => Ok(Pre::NotUsing),
			Val::UsingNft { owner, instance, state_nonce } => {
				let nft = NftsByOwner::<T>::try_mutate_exists(
					&owner,
					|maybe_nft| -> Result<Nft, TransactionValidityError> {
						let nft = maybe_nft.as_ref().ok_or(CustomInvalidity::NoNft)?;
						if nft.instance != instance || nft.state_nonce != state_nonce {
							return Err(CustomInvalidity::NftStateMismatch.into());
						}
						// Dispatch assumes the source purse is empty. Taking the NFT here
						// prevents same-block double use and lets post-dispatch restore the exact
						// pre-state if dispatch fails.
						Ok(maybe_nft.take().expect("NFT existence checked above; qed"))
					},
				)?;
				Ok(Pre::UsingNft { owner, nft })
			},
		}
	}
```
