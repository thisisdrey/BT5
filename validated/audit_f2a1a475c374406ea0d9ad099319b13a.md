### Title
Unprivileged, consent-free NFT minting into arbitrary purse keys enables permanent griefing lock via `pallet-scarcity`'s `Locked` backoff mechanism - (`substrate/frame/scarcity/src/lib.rs`, `substrate/frame/scarcity/src/extension.rs`)

### Summary
The Stakehouse report's core broken invariant is: an unprivileged third party can perform a cheap state-mutating action *targeting a victim's account* that a downstream cooldown/guard reads, causing the victim's legitimate, otherwise-eligible operation to revert. `pallet-scarcity` reproduces the same broken-invariant shape: any signed account can permissionlessly create a collection and then `mint` an NFT into *any account's* purse key with zero destination consent, and that occupied purse key causes a subsequent legitimate `transfer` targeting it to fail. The resulting dispatch failure feeds `pallet-scarcity`'s exponential `Locked` backoff on the *sender's own account*, not the attacker's, letting a low-cost, unprivileged, repeatable action escalate into an ever-growing lockout of an unrelated victim's purse key.

### Finding Description
`pallet-scarcity` implements a "coinage" model where each account (`purse key`) can hold at most one NFT, tracked in `NftsByOwner<T>` [1](#0-0) .

The pallet's own documentation confirms there is no destination consent check for minting or force-transferring: [2](#0-1) 

`create_collection` is open to any signed account with no privilege requirement [3](#0-2) , and `mint` lets that self-appointed "collection owner" place an instance into **any** empty purse key of their choosing, including a victim's account, with no opt-in from the victim [4](#0-3) .

Ordinary, holder-initiated transfers explicitly reject a busy destination: [5](#0-4) 

These `AsScarcity`-authorized `transfer`/`burn` calls are dispatched through purse-key origin (not system-account-nonce authorized), and any dispatch failure is caught by the `AsScarcity` transaction extension's `post_dispatch_details`, which restores pre-state and applies an **exponentially growing** backoff lock keyed by the account whose purse key attempted the dispatch: [6](#0-5) [7](#0-6) 

Because `AsScarcity`-signed transactions are validated and prepared against `NftsByOwner` state that any unprivileged third party can mutate at will via `mint` into a not-yet-occupied purse key, an attacker can pre-occupy a victim's *sending* purse key's intended destination (or race a soon-to-be-empty key) so that the victim's own signed, otherwise valid `transfer` transaction fails at dispatch with `AddressOccupied`. That failure does not just revert harmlessly — it drives `failed_dispatch_lock`, whose lock duration is `2^retries * LockPeriod` (capped only at `retries.min(63)`), meaning repeated griefing by the attacker (re-minting into the freshly emptied destination the moment the victim's `Locked` lock expires and they retry) compounds the victim's lockout exponentially, effectively freezing the victim's purse key from any further NFT movement indefinitely at negligible, unbounded-repeatable cost to the attacker relative to the victim's escalating cost.

This is structurally identical to the Stakehouse bug: an unrelated, unprivileged party mutates state tied to a victim's key (`lastInteractedTimestamp` there, `NftsByOwner` occupancy here) that a separate cooldown/guard check (`_assertUserHasEnoughGiantLPToClaimVaultLP`'s day check there, the exponential `Locked` backoff here) uses to gate or punish the victim's legitimate withdrawal/transfer.

### Impact Explanation
A victim's purse key can be driven into an exponentially growing, effectively permanent transaction lockout by an unprivileged attacker repeatedly re-occupying the destination purse key the moment it becomes free, causing every retried legitimate `transfer`/`burn` to fail and further doubling the lockout period each time. This is a permanent user-fund/state lock: the victim's NFT (a value-bearing, deposit-backed asset per `InstanceDeposits<T>`) becomes practically un-transferable and the account becomes unusable for the pallet's intended purse-key transaction flow, with no admin, governance, relayer, or peer compromise required.

### Likelihood Explanation
The attack requires only: (1) permissionlessly creating a collection (`create_collection`), (2) minting a low-cost instance definition, and (3) repeatedly calling `mint` targeting the victim's known purse-key address whenever it is briefly empty. All of these are ordinary, unprivileged, publicly available extrinsics; no validator/collator/relayer collusion, leaked keys, or governance action is needed. The exponential backoff (`2u64.saturating_pow(exponent.min(63))`) guarantees the griefing compounds without bound after a handful of induced failures.

### Recommendation
Require explicit destination consent (or an allow-list / opt-in flag) before `mint`/`force_transfer` can place an instance into a purse key the account did not request, and decouple the `Locked` backoff so a dispatch failure caused by third-party interference with the *destination* does not penalize the *sender's own* purse key; alternately, do not treat `AddressOccupied` failures as attributable retries for `failed_dispatch_lock` purposes.

### Proof of Concept
1. Attacker calls `create_collection`, `define_item`, then `mint(origin=attacker, collection, item, to=victim, metadata=[])`, filling `victim`'s purse key in `NftsByOwner`.
2. `Bob` has a pre-authorized, mortal `AsScarcity`-signed `transfer` transaction sending an NFT to `victim`. When this transaction dispatches, `Pallet::transfer` bails with `AddressOccupied` (`ensure!(!NftsByOwner::<T>::contains_key(&to), Error::<T>::AddressOccupied)`), and since `Bob`'s own purse key was already emptied in `prepare`, `post_dispatch_details` restores `Bob`'s NFT but inserts `Locked::<T>::insert(&Bob, failed_dispatch_lock(...))`.
3. As soon as `Locked` on `Bob` expires and/or `victim` burns the unwanted junk NFT (freeing the slot), the attacker immediately re-mints into `victim`'s purse key before `Bob`'s retried transfer lands, repeating step 2 and doubling `Bob`'s lock duration each cycle (`retries` increments every failure).
4. After a small number of induced failures, `Bob`'s purse key is locked out for an exponentially large duration, unable to move his NFT, while the attacker's cost stays constant (repeated cheap mint/burn cycles against `victim`'s or their own throwaway collections).

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

**File:** substrate/frame/scarcity/src/lib.rs (L311-313)
```rust
	/// One NFT per owner key — the coinage model.
	#[pallet::storage]
	pub type NftsByOwner<T: Config> = StorageMap<_, Blake2_128Concat, T::AccountId, Nft>;
```

**File:** substrate/frame/scarcity/src/lib.rs (L542-547)
```rust
		#[pallet::call_index(0)]
		#[pallet::weight(T::WeightInfo::create_collection())]
		pub fn create_collection(origin: OriginFor<T>) -> DispatchResult {
			let owner = ensure_signed(origin)?;
			Self::do_create_collection(owner).map(|_| ())
		}
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

**File:** substrate/frame/scarcity/src/lib.rs (L590-605)
```rust
		pub fn transfer(origin: OriginFor<T>, to: T::AccountId) -> DispatchResultWithPostInfo {
			let Ok(Origin::Nft { owner, nft }) = origin.into() else {
				return Err(DispatchError::BadOrigin.into());
			};
			ensure!(to != owner, Error::<T>::SelfTransfer);
			ensure!(!NftsByOwner::<T>::contains_key(&to), Error::<T>::AddressOccupied);

			let from = owner;
			let state_nonce =
				nft.state_nonce.checked_add(1).ok_or(Error::<T>::StateNonceOverflow)?;
			let nft = Nft { last_moved: T::UnixTime::now().as_secs(), state_nonce, ..nft };
			NftsByOwner::<T>::insert(&to, nft.clone());
			Instances::<T>::insert(nft.instance, &to);
			Self::deposit_event(Event::Transferred { instance: nft.instance, from, to });
			Ok(Pays::No.into())
		}
```

**File:** substrate/frame/scarcity/src/extension.rs (L163-173)
```rust
	fn failed_dispatch_lock(previous: Option<LockInfo>) -> LockInfo {
		let retries = previous.map(|lock| lock.retries.saturating_add(1)).unwrap_or(1);
		let exponent = retries.saturating_sub(1);
		let multiplier = 2u64.saturating_pow(u32::from(exponent).min(63));
		LockInfo {
			retries,
			until: T::UnixTime::now()
				.as_secs()
				.saturating_add(multiplier.saturating_mul(T::LockPeriod::get())),
		}
	}
```

**File:** substrate/frame/scarcity/src/extension.rs (L283-299)
```rust
	fn post_dispatch_details(
		pre: Self::Pre,
		_info: &DispatchInfoOf<<T as frame_system::Config>::RuntimeCall>,
		_post_info: &PostDispatchInfoOf<<T as frame_system::Config>::RuntimeCall>,
		_len: usize,
		result: &DispatchResult,
	) -> Result<Weight, TransactionValidityError> {
		if let Pre::UsingNft { owner, nft } = pre {
			if result.is_err() {
				NftsByOwner::<T>::insert(&owner, nft);
				Locked::<T>::insert(&owner, Self::failed_dispatch_lock(Locked::<T>::get(&owner)));
			} else {
				Locked::<T>::remove(&owner);
			}
		}
		Ok(Weight::zero())
	}
```
