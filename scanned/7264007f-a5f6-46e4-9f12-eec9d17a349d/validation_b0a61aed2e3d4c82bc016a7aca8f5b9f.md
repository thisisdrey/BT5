## Analysis

The external report's core pattern: a state-mutating entry point takes an attacker-controlled destination/beneficiary parameter (`_to`) with no validation or consent check from that destination, letting the caller alter another user's permission/allowance state and block them.

The closest local analog is in the newly-added `pallet-scarcity` (a "coinage-style" one-NFT-per-account collectible pallet), specifically its `mint` flow, `do_mint`/`do_mint_inner`.

### Title
Unvalidated `to` destination in `pallet-scarcity::mint` lets any collection owner permanently occupy an arbitrary victim's global one-NFT-per-purse slot - (File: `substrate/frame/scarcity/src/lib.rs`)

### Summary
`pallet-scarcity` enforces a global invariant of "one instance per fresh purse public key" via a single, collection-agnostic map `NftsByOwner<T>`. `Pallet::do_mint` only checks that the *caller* is the owner of the collection being minted from; it performs no validation or consent check on the `to` account that will receive the instance. [1](#0-0) 

### Finding Description
`do_mint` gates only the caller's authority over the *collection*:
```
ensure!(info.owner == owner, Error::<T>::NoPermission);
Self::do_mint_inner(collection, item, to, metadata, true)
``` [2](#0-1) 

`do_mint_inner` then enforces the one-purse-per-account rule purely as a failure guard against an *already occupied* destination, with no opt-in/consent requirement from `to` before writing to its slot:
```
ensure!(!NftsByOwner::<T>::contains_key(&to), Error::<T>::AddressOccupied);
...
NftsByOwner::<T>::insert(&to, nft);
Instances::<T>::insert(instance, &to);
``` [3](#0-2) 

`NftsByOwner` is a single, non-collection-scoped map, confirmed by the pallet doc comment describing "one instance per fresh purse public key" as a pallet-wide (not per-collection) rule, and by tests that show `mint` calls succeeding/failing purely based on whether `to` already holds any instance, regardless of collection:
```
mint(0, RECIPIENT);
assert_noop!(
    Scarcity::mint(RuntimeOrigin::signed(OWNER), 0, 1, RECIPIENT, metadata(&[])),
    Error::<Test>::AddressOccupied
);
``` [4](#0-3) 

Because `to` is a plain `T::AccountId` parameter with no signature or `ensure_signed(to)`-style consent, any account that controls a collection (collections themselves are cheap/permissionless-style user objects backed only by a storage deposit ticket, not root/governance-gated) can call `mint` and name an arbitrary victim account as `to`. This differs from the FantiumMinterV1 bug only in mechanism, not in class: an entry point that requires *some* authorization for the caller's own action, but applies none to the third-party account it writes into — letting the caller unilaterally alter that third party's protocol-level state (here, "does this purse hold an NFT") without their consent.

### Impact Explanation
Because the "one instance per purse" rule is global across the whole pallet (not scoped to one collection), an attacker who mints one junk instance into a victim's account permanently prevents that account from receiving **any** other NFT from **any** collection under this pallet until the victim notices and burns the unwanted instance via `AsScarcity`/`burn`. This is a direct "block any user" griefing primitive: a legitimate collection owner trying to airdrop/mint to a set of users can be pre-empted by an attacker occupying those same target accounts first, causing `AddressOccupied` failures for the legitimate mint and denying honest recipients access to intended NFTs — the same "no `onlyPlatformManager`-style protection on the target account" root cause as the source report.

### Likelihood Explanation
Any account able to create a collection and define an item (a self-service, deposit-backed operation, not a root/governance action) can immediately weaponize this against arbitrary victim addresses with a single `mint` call per victim; no privileged role, validator/collator collusion, or leaked keys are required. The guard that exists (`AddressOccupied`) only prevents double-occupation, it never asks the destination account to consent to being occupied in the first place.

### Recommendation
Require the destination purse to opt in to occupation — e.g., only allow `to == caller` for self-mint, or require a signed acceptance/pre-signed authorization from `to` (similar to the pallet's own `mint_pre_signed`-style pattern used elsewhere in this codebase, e.g. `pallet-nfts`'s `only_account` witness check), before writing into `NftsByOwner<T>` for an account that did not initiate the mint.

### Proof of Concept
1. Attacker calls `Scarcity::create(...)` to become owner of collection `C` (self-service, deposit-backed).
2. Attacker calls `Scarcity::define(C, item, ...)`.
3. Attacker calls `Scarcity::mint(origin=attacker, C, item, to=VICTIM, metadata=[])`. This succeeds without any signature or consent from `VICTIM`, per `do_mint`/`do_mint_inner`. [5](#0-4) 
4. `NftsByOwner::<T>::get(VICTIM)` is now `Some(_)`. Any subsequent, legitimate `mint` call by a different, honest collection owner targeting `VICTIM` fails with `Error::<T>::AddressOccupied`, as shown in the existing test:
```
mint(0, RECIPIENT);
assert_noop!(Scarcity::mint(..., RECIPIENT, ...), Error::<Test>::AddressOccupied);
``` [4](#0-3) 
5. `VICTIM` is blocked from receiving any intended NFT until they discover and burn the unwanted instance themselves.

**Note on uncertainty:** I was not able to view the exact `#[pallet::call]` signature/origin checks for `mint` and `create` (only `do_mint`/`do_mint_inner` internals and test call-sites were retrieved before the tool budget ran out), so I cannot 100% confirm from source that `create`/`mint` are fully permissionless for arbitrary signed accounts versus gated by some additional origin filter in the runtime configuration. This should be verified directly against the pallet's `#[pallet::call]` block and the runtime's `Config` for `pallet-scarcity` before treating this as fully confirmed.

### Citations

**File:** substrate/frame/scarcity/src/lib.rs (L1261-1272)
```rust
		/// Mint an instance after enforcing collection ownership and the one-NFT-per-key rule.
		pub fn do_mint(
			owner: T::AccountId,
			collection: CollectionId,
			item: ItemIndex,
			to: T::AccountId,
			metadata: Vec<(MetadataKeyOf<T>, MetadataValueOf<T>)>,
		) -> Result<InstanceId, DispatchError> {
			let info = Collections::<T>::get(collection).ok_or(Error::<T>::UnknownCollection)?;
			ensure!(info.owner == owner, Error::<T>::NoPermission);
			Self::do_mint_inner(collection, item, to, metadata, true)
		}
```

**File:** substrate/frame/scarcity/src/lib.rs (L1290-1325)
```rust
			ensure!(!NftsByOwner::<T>::contains_key(&to), Error::<T>::AddressOccupied);

			let instance = NextInstanceId::<T>::get();
			let next_instance = instance.checked_add(1).ok_or(Error::<T>::TooManyInstances)?;
			let next_supply = definition.supply.checked_add(1).ok_or(Error::<T>::SupplyOverflow)?;
			let next_live_supply =
				definition.live_supply.checked_add(1).ok_or(Error::<T>::SupplyOverflow)?;
			let now = T::UnixTime::now().as_secs();
			let nft =
				Nft { instance, collection, item, minted_at: now, last_moved: now, state_nonce: 0 };
			let instance_deposit = if with_deposit {
				// Four storage entries back one instance: `NftsByOwner`, the `Instances`
				// reverse index, `InstanceDeposits`, and `InstanceMetadataCount`. This measures
				// their logical encoded payload; the runtime's per-record base can price trie
				// and hash-prefix overhead.
				let record_size = nft
					.encoded_size()
					.saturating_add(to.encoded_size().saturating_mul(2))
					.saturating_add(instance.encoded_size().saturating_mul(3))
					.saturating_add(BalanceOf::<T>::max_encoded_len())
					.saturating_add(u32::max_encoded_len());
				let deposit = T::InstanceDeposit::convert(Footprint::from_parts(4, record_size));
				info = Self::increase_owner_deposit(info, deposit)?;
				Some(deposit)
			} else {
				None
			};

			definition.supply = next_supply;
			definition.live_supply = next_live_supply;
			NextInstanceId::<T>::put(next_instance);
			let collection_owner = info.owner.clone();
			Collections::<T>::insert(collection, info);
			ItemDefs::<T>::insert(collection, item, definition);
			NftsByOwner::<T>::insert(&to, nft);
			Instances::<T>::insert(instance, &to);
```

**File:** substrate/frame/scarcity/src/tests.rs (L913-923)
```rust
#[test]
fn mint_enforces_one_nft_per_key() {
	new_test_ext().execute_with(|| {
		setup_item();
		define(0);
		mint(0, RECIPIENT);
		assert_noop!(
			Scarcity::mint(RuntimeOrigin::signed(OWNER), 0, 1, RECIPIENT, metadata(&[])),
			Error::<Test>::AddressOccupied
		);
	});
```
