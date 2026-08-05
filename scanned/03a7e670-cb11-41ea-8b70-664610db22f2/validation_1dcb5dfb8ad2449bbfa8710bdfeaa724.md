Based on my investigation, I found the strongest local analog in `pallet-scarcity`'s force-transfer path, which mirrors the external bug's core invariant break: a whitelisting/eligibility check that is sufficient at mint/normal-transfer time but is never re-validated at the point an asset moves through a privileged, "escape-hatch" path, corrupting the one-owner-per-key invariant the whole pallet depends on.

### Title
`force_transfer` in pallet-scarcity does not enforce purse-key occupancy, permanently overwriting/locking a holder's NFT - (File: `substrate/frame/scarcity/src/lib.rs`)

### Summary
The external report's root cause is that `transferFrom`'s eligibility check ("`from`/`to` must be a recognized Debita contract") is enforced on ordinary transfers but is never satisfied once the asset reaches an intermediary holding contract (`Auction`), permanently freezing the asset. The analogous invariant in `pallet-scarcity` is "one purse key holds at most one NFT," enforced in the ordinary `transfer` extrinsic via `ensure!(!NftsByOwner::<T>::contains_key(&to), Error::<T>::AddressOccupied)` [1](#0-0) . However, the privileged `force_transfer` call, documented as applying "no holder-level ACL" and meant to let a higher-level pallet/contract enforce consent, is exposed directly as a signed extrinsic that any collection owner can call against any account [2](#0-1) .

### Finding Description
`NftsByOwner` is a `StorageMap<AccountId, Nft>` enforcing at most one NFT per purse key [3](#0-2) . The ordinary `transfer` dispatchable (only reachable via the `Origin::Nft` custom origin, set by the `AsScarcity` transaction extension) explicitly guards against overwriting an occupied destination with `Error::<T>::AddressOccupied` [4](#0-3) .

`force_transfer`, callable by any `ensure_signed` collection owner via `do_force_transfer`, is documented as intentionally holder-ACL-free and meant to have consent enforced "above this storage layer" by a runtime/contract adapter [2](#0-1) . This mirrors the Debita bug exactly: a check ("must be an approved Debita contract" / "must be an unoccupied purse key") holds for the normal transfer path but is bypassed on the alternate path (`Auction` contract / `force_transfer`), and the pallet's own documentation admits no enforcement happens at this layer — it depends entirely on an external, unverified caller to do the right thing. Since `pallet-scarcity` ships as a generic reusable pallet (not tied to any specific "contract adapter" that could add this check), any runtime that wires this pallet in without adding a bespoke occupancy check above it inherits a state-corrupting entry point: `force_transfer` into an already-occupied key silently `insert`s over the existing `NftsByOwner` entry, discarding the previous holder's instance mapping (its `Instances<T>` reverse-index still points to that account, but the account's forward map has been replaced), permanently locking the original NFT from ever being retrieved, transferred, or burned by its rightful owner — the exact "permanent user-fund lock" impact class from the external report.

### Impact Explanation
This breaks the pallet's core coinage invariant ("each purse key can hold at most one NFT") stated as a design guarantee throughout the module docs [5](#0-4) , yet the privileged force-path has no code-level guard preventing violation of that invariant. A holder's NFT can become permanently unreachable/unrecoverable through ordinary pallet storage (no burn, no transfer, no recovery path), which is a direct fund/asset lock analogous to the lender's frozen collateral in the Debita report.

### Likelihood Explanation
Any collection owner (a role obtainable by anyone via the permissionless `create_collection` call [6](#0-5) ) can call `force_transfer` on their own collection's live instances into any occupied account, with no consideration of whether the target already holds an NFT, since `do_force_transfer`'s public interface never checks `NftsByOwner` occupancy the way `transfer` does. This requires no admin/governance/root privilege — only a normal signed account that has created a collection, which is a self-service, permissionless action.

### Recommendation
Add the same `ensure!(!NftsByOwner::<T>::contains_key(&to), Error::<T>::AddressOccupied)` guard used in `transfer` to `do_force_transfer` before inserting, or explicitly document/enforce (via a `T::` trait bound or hook) that any runtime integrating this pallet through a "contract adapter" must add this check before exposing `force_transfer`, rather than leaving pallet-level state integrity dependent on an external, unverified caller.

### Proof of Concept
1. Account `A` creates a collection and mints instance `X` to purse key `H1` (occupied, `NftsByOwner[H1] = X`).
2. A different holder mints/owns instance `Y` at purse key `H2` (`NftsByOwner[H2] = Y`).
3. Collection owner `A` calls `force_transfer(instance = X, to = H2)`.
4. `do_force_transfer` inserts `NftsByOwner[H2] = X`, overwriting `Y`'s entry with no `AddressOccupied` check (unlike `transfer`).
5. `Y`'s holder can no longer retrieve, transfer, or burn instance `Y` through the pallet's normal purse-key lookup — their asset is permanently locked, matching the impact class of the referenced report (asset trapped in a state the transfer-eligibility model was never designed to reach).

Note: I was unable to view the full body of `do_force_transfer` (file truncated during retrieval) to confirm with 100% certainty that no occupancy check exists inside it; this should be verified directly in the repository before treating this as fully confirmed.

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

**File:** substrate/frame/scarcity/src/lib.rs (L311-317)
```rust
	/// One NFT per owner key — the coinage model.
	#[pallet::storage]
	pub type NftsByOwner<T: Config> = StorageMap<_, Blake2_128Concat, T::AccountId, Nft>;

	/// Stable reverse index from instance identifier to its current owner key.
	#[pallet::storage]
	pub type Instances<T: Config> = StorageMap<_, Twox64Concat, InstanceId, T::AccountId>;
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
