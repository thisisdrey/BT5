Based on my investigation, I found a concrete local analog in `pallet-scarcity` (`substrate/frame/scarcity`), a custom NFT "purse" pallet included in this fork.

### Title
Permissionless purse-squatting via unconsented `mint()` permanently blocks legitimate NFT delivery to a victim account - (File: substrate/frame/scarcity/src/lib.rs)

### Summary
The Cachet/Coinage-style bug class in the C4 report is: a transfer-back path that must succeed unconditionally can be permanently blocked because the destination cannot be forced to accept the incoming asset, freezing the asset in place. `pallet-scarcity` has a structurally identical failure mode in its "one NFT per purse key" model: any unprivileged account can occupy *another* account's empty purse key without that account's consent, permanently blocking any subsequent legitimate mint/transfer/payout that targets that same purse key.

### Finding Description
`pallet-scarcity` enforces a strict coinage invariant — each account (`purse key`) can hold at most one NFT, tracked in `NftsByOwner<T>` [1](#0-0) . Both the holder-initiated `transfer` and (by design intent, per the doc comments) `mint` reject any destination that already holds an instance, via `Error::<T>::AddressOccupied` [2](#0-1) [3](#0-2) .

Critically, `mint()` requires **no consent from the destination account** — any account that has created a collection (a permissionless, cheap operation via `create_collection`) can mint an item into *any* other account's empty purse key. The module documentation explicitly acknowledges this: “Any collection owner can mint into … any empty purse key, and because each key holds at most one NFT, an unsolicited instance blocks that key from receiving anything else until its holder burns it or transfers it away.” [4](#0-3) 

This is the direct structural analog of the ERC721 `safeTransferFrom` bug: a *delivery-completion* path (moving value/an asset to a specific beneficiary account) can be unconditionally and permanently blocked by an action the beneficiary does not control, because the receiving slot enforces an occupancy invariant with no override or recovery path for the intended recipient other than the squatter's own future action.

### Impact Explanation
Any runtime that uses `pallet-scarcity::mint` as a payout/reward/settlement primitive (e.g., delivering a reward NFT, a ticket, or a game asset to a purse key) is exposed: an unprivileged attacker can pre-occupy the victim's purse key with a throwaway collection/item at negligible cost, permanently preventing the intended payout from landing (`AddressOccupied`) until the victim notices and manually burns or transfers away the unwanted item. This is a "permanent user-fund/asset lock" / griefing of settlement finality — the legitimate payout can never complete against that purse key without out-of-band victim action, mirroring the "service becomes permanently irrecoverable" impact of the seed report, except here it blocks *delivery* rather than *return*.

### Likelihood Explanation
Likelihood is comparable to the original C4 Medium finding: it requires no privileged role, no validator/collator/relayer compromise, and no governance action — only calling `create_collection` (permissionless) and `mint` into the target's purse. The pallet's own doc explicitly flags this as a known, accepted design tradeoff ("Holders should treat purse keys as disposable... runtimes... must enforce [receive-consent] policy above this storage layer"), which parallels Olas's own disposition of M-16 as an acknowledged, reduced-severity issue rather than a "fixed-must" bug — i.e., it is real, exploitable, and already documented as a caveat rather than eliminated.

### Recommendation
Any runtime pallet or contract that uses `pallet-scarcity::mint`/`transfer` to deliver value to a specific beneficiary (rewards, treasury payouts, staking settlement, etc.) must not target end-user purse keys directly without an explicit opt-in/reservation mechanism (e.g., a per-recipient "expected instance" claim ticket), or must provide a privileged force-clear/force-burn recovery path that the intended recipient — not just the collection owner — can invoke. At minimum, this caveat needs to be enforced as a hard requirement (not just documentation) at the integration layer before this pallet is wired into any payout/settlement flow.

### Proof of Concept
1. Victim `V` holds no NFT (empty purse) and is expected to receive a reward via `Scarcity::mint(collection_R, item_R, V, ...)` from a legitimate rewards collection `R`.
2. Attacker `A` (unprivileged) calls `Scarcity::create_collection()`, then `Scarcity::define_item(...)`, then `Scarcity::mint(collection_A, item_A, V, ...)` — this succeeds because `V`'s purse is currently empty and `mint` requires no consent [5](#0-4) .
3. `V`'s purse (`NftsByOwner<T>`) now holds attacker's junk NFT.
4. The legitimate reward mint `Scarcity::mint(collection_R, item_R, V, ...)` now fails with `Error::AddressOccupied` [2](#0-1) , permanently blocking delivery until `V` notices and manually burns/transfers away the squatting instance.

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

**File:** substrate/frame/scarcity/src/lib.rs (L420-422)
```rust
		DeletionInvariant,
		/// The destination purse key already holds an NFT.
		AddressOccupied,
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

**File:** substrate/frame/scarcity/src/lib.rs (L590-595)
```rust
		pub fn transfer(origin: OriginFor<T>, to: T::AccountId) -> DispatchResultWithPostInfo {
			let Ok(Origin::Nft { owner, nft }) = origin.into() else {
				return Err(DispatchError::BadOrigin.into());
			};
			ensure!(to != owner, Error::<T>::SelfTransfer);
			ensure!(!NftsByOwner::<T>::contains_key(&to), Error::<T>::AddressOccupied);
```
