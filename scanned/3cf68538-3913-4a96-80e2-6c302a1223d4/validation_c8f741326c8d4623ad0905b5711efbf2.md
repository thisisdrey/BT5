### Title
Unsolicited, consent-free NFT minting into any purse key permanently locks keyless (pallet/derived) accounts out of `pallet-scarcity` — ([File: substrate/frame/scarcity/src/lib.rs])

### Summary
`pallet-scarcity` implements the same "force state onto an unwilling target" primitive described in the PRBProxyRegistry report: any unprivileged account can permissionlessly mint an NFT into *any* account it chooses, with no receiver consent, and the only guard (`AddressOccupied`) protects against overwriting an existing NFT — it does nothing to stop the attacker from being the first to occupy a target address. Because `pallet-scarcity`'s only way to vacate a purse key is a *signed* transaction from that key via the `AsScarcity` extension, an attacker can permanently deny a keyless/derived account (e.g. a pallet sovereign account, pool account, or treasury-style account used by a higher-level integration through `MintWithoutDeposit`) the ability to ever legitimately hold an instance.

### Finding Description
`mint` and `MintWithoutDeposit::mint_without_deposit` both route through `do_mint_inner`, whose only anti-collision guard is: [1](#0-0) 

This is structurally identical to PRBProxyRegistry's `noProxy()` modifier: it prevents *overwriting* an occupied slot but does not require the destination's consent to be occupied in the first place. `mint` is a fully permissionless, unprivileged public entrypoint — any signed account may `create_collection`, `define_item`, and then `mint` an instance to an arbitrary `to: T::AccountId` of its choosing: [2](#0-1) 

The module documentation itself confirms this is a "no destination consent" design and that any empty purse key is a valid target: [3](#0-2) 

The critical gap is what happens when the occupied address is *keyless* — a PalletId-derived account, treasury/reward-vault account, or any other well-known deterministic address that a higher-level runtime pallet intends to mint into later via `MintWithoutDeposit`: [4](#0-3) 

The only two ways to vacate a purse key — `transfer` and `burn` — both require an `Origin::Nft`, which `AsScarcity::validate` only produces from a *Signed* origin matching the purse key's own private key: [5](#0-4) 

A pallet-derived account has no private key and can never submit such a signed extrinsic. The only other path to free the slot is `force_burn`/`force_transfer`, but those require being the *collection owner* of the collection that minted the squatting instance — a role the attacker controls and will never exercise against themselves: [6](#0-5) 

So the existing "already occupied" guard, exactly like PRBProxyRegistry's `noProxy()`, only prevents accidental double-assignment — it does not stop an attacker from being the one to occupy the slot first, and unlike the Solidity case there is no way at all for the keyless victim to recover, since it cannot sign a transaction to reclaim its own slot.

### Impact Explanation
Any runtime that wires a game/rewards/treasury pallet to `pallet-scarcity`'s `MintWithoutDeposit` trait to allocate instances to protocol-controlled accounts (vaults, pool accounts, staking-reward accounts, etc.) is exposed to a permanent, irreversible denial-of-service: an unprivileged attacker front-runs (or simply pre-runs, since no specific victim transaction needs to be targeted) the legitimate mint by calling `create_collection` → `define_item` → `mint` into the known deterministic target address. Every subsequent legitimate `mint`/`mint_without_deposit` into that address fails with `AddressOccupied` forever, because the keyless account can never sign a `transfer`/`burn` to clear itself, and the attacker (as sole collection owner of the squatting instance) has no incentive to force-burn it. This is a permanent protocol-state lock on the affected account, not a transient griefing condition recoverable by the victim.

### Likelihood Explanation
High: `create_collection`, `define_item`, and `mint` are all unprivileged, feeless-adjacent, ordinary signed extrinsics available to any account with minimal balance for deposits. No validator, collator, governance, or admin action is required, and the target address (any PalletId-derived or otherwise deterministic account used by a higher-level integration) is public knowledge, computable off-chain in advance — the attack does not even need to observe a specific mempool transaction to front-run.

### Recommendation
Require destination consent for `mint`/`mint_without_deposit`, or make the guard direction-aware: e.g., allow the pallet using `MintWithoutDeposit` to pre-reserve/whitelist specific target accounts, allow the affected keyless account's *owning pallet* to force-clear/force-burn regardless of which collection minted into it, or add a root/governance-independent "beneficiary escape hatch" that lets the true controller of a derived account reclaim its purse key without needing to countersign as `Origin::Nft`. At minimum, document prominently (and enforce at the integration layer) that `MintWithoutDeposit` must never target a deterministic/keyless account without a corresponding pre-emptive occupancy check performed atomically with account derivation.

### Proof of Concept
1. Runtime integrates `pallet-scarcity` and a hypothetical `pallet-rewards` that uses `MintWithoutDeposit::mint_without_deposit` to mint a "reward NFT" into its `PalletId::get().into_account_truncating()` vault account the first time rewards accrue.
2. Attacker computes this deterministic account off-chain (trivial, since `PalletId` and the derivation algorithm are public).
3. Attacker calls `Scarcity::create_collection`, `Scarcity::define_item`, then `Scarcity::mint(collection, item, vault_account, [])`, per [7](#0-6) , succeeding because `NftsByOwner::<T>::contains_key(&vault_account)` is currently `false`.
4. `pallet-rewards`'s later call to `mint_without_deposit(..., vault_account, ...)` now unconditionally fails with `Error::<T>::AddressOccupied` at [8](#0-7) .
5. `vault_account` has no private key, so it can never submit an `AsScarcity`-validated `transfer`/`burn` to free itself (blocked at [9](#0-8) ), and the attacker, as sole owner of the squatting collection, will not call `force_burn`/`force_transfer` on their own instance — permanently disabling reward-NFT issuance for that vault.

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

**File:** substrate/frame/scarcity/src/lib.rs (L776-792)
```rust
		/// Force-transfer one live instance as its collection owner.
		///
		/// The collection layer intentionally applies no holder-level ACL. When a runtime exposes
		/// this call to its contract environment, a contract-owned collection can enforce its own
		/// consent and game rules before calling it. The move increments the instance state nonce,
		/// invalidating prior holder authorizations.
		#[pallet::call_index(13)]
		#[pallet::weight(T::WeightInfo::force_transfer())]
		#[transactional]
		pub fn force_transfer(
			origin: OriginFor<T>,
			instance: InstanceId,
			to: T::AccountId,
		) -> DispatchResult {
			let owner = ensure_signed(origin)?;
			Self::do_force_transfer(&owner, instance, to)
		}
```

**File:** substrate/frame/scarcity/src/lib.rs (L1286-1290)
```rust
			let mut info =
				Collections::<T>::get(collection).ok_or(Error::<T>::UnknownCollection)?;
			let mut definition =
				ItemDefs::<T>::get(collection, item).ok_or(Error::<T>::UnknownItem)?;
			ensure!(!NftsByOwner::<T>::contains_key(&to), Error::<T>::AddressOccupied);
```

**File:** substrate/frame/scarcity/src/lib.rs (L1344-1357)
```rust
	impl<T: Config> crate::MintWithoutDeposit<T::AccountId> for Pallet<T> {
		type MetadataKey = MetadataKeyOf<T>;
		type MetadataValue = MetadataValueOf<T>;

		fn mint_without_deposit(
			collection: CollectionId,
			item: ItemIndex,
			to: T::AccountId,
			metadata: Vec<(Self::MetadataKey, Self::MetadataValue)>,
		) -> Result<InstanceId, DispatchError> {
			ensure!(Collections::<T>::contains_key(collection), Error::<T>::UnknownCollection);
			Self::do_mint_inner(collection, item, to, metadata, false)
		}
	}
```

**File:** substrate/frame/scarcity/src/extension.rs (L215-218)
```rust
		let Some(frame_system::Origin::<T>::Signed(owner)) = origin.as_system_ref() else {
			return Err(CustomInvalidity::OriginToAsNftMustBeSigned.into());
		};
		let owner = owner.clone();
```
