### Title
Self-Minted NFT Purse Grants Perpetual Fee-Free `transfer`/`burn` Dispatch in `pallet-scarcity`, Enabling Underpriced Block-Weight Spam - (File: `substrate/frame/scarcity/src/lib.rs`, `substrate/frame/scarcity/src/extension.rs`)

### Summary
`pallet-scarcity`'s `transfer` and `burn` calls are marked `#[pallet::feeless_if(is_nft_origin)]`, so any dispatch carrying the pallet's custom `Origin::Nft` is charged `Pays::No` regardless of caller [1](#0-0) . The only gate that decides whether a transaction is allowed to switch into `Origin::Nft` is the `AsScarcity` transaction extension checking that the signer currently owns *some* NFT instance [2](#0-1) . Since collection creation and minting are permissionless (any signed account may `create_collection` and then `mint` into any empty purse key it controls) [3](#0-2) , an attacker can self-mint one NFT and then indefinitely bounce it between two purse keys it controls, submitting an unbounded stream of `Pays::No` transactions that still fully consume block weight — the same "grant yourself the exemption, then abuse the fee-free path" pattern as the Vether `addExcluded` bug, just gated by a one-time deposit instead of being entirely free.

### Finding Description
`Origin::Nft` is a privileged, fee-exempt dispatch origin: any call reaching the pallet under this origin (`transfer`, `burn`) bypasses `pallet-transaction-payment` fees via `feeless_if` [4](#0-3) [5](#0-4) . The gate into that origin is `AsScarcity::validate`, which requires the caller to currently hold an NFT recorded in `NftsByOwner` matching the claimed `instance`/`state_nonce` [6](#0-5) .

Crucially, nothing restricts *who* can become an NFT holder: `create_collection` is open to any signed account, and the resulting collection owner can then `mint` new instances into any empty purse key (including its own), with no destination-consent check ("Purse keys ... apply no destination consent") [7](#0-6) [8](#0-7) . After one deposit-bearing mint, the attacker owns a permanent fee-exemption token: it can sign a `transfer` moving the NFT from purse A to purse B (both attacker-controlled), which is fee-free (`Pays::No`), then immediately do the reverse transfer once the new `state_nonce` is known, repeating indefinitely. Each `transfer` still costs full `T::WeightInfo::transfer()` block weight plus the extension's `as_scarcity_pipeline()` weight [9](#0-8) , but the caller pays nothing.

This mirrors the Vether flaw's core primitive exactly: a public, unprivileged action (there: `addExcluded(self)`; here: `create_collection` + `mint` to self) unlocks membership in a set (`ExcludedAddresses` / effectively "NFT-holder feeless class") that a fee-charging mechanism (`Vether` transfer fee / `pallet-transaction-payment` via `feeless_if`) is supposed to gate to trusted/limited participants, but instead the set is open to anyone willing to pay a one-time, non-scaling cost. The existing guards (`AsScarcity`'s ownership/state-nonce check, replay/mortality rules, backoff lock on failure) only ensure the *NFT bookkeeping* stays consistent — they do nothing to bound how many free, weight-consuming transactions a single self-minted NFT can generate over time.

### Impact Explanation
An attacker who mints a single NFT (paying only the one-time storage deposit) obtains an indefinitely reusable channel to submit `Pays::No` transactions that still occupy full block weight and storage-write bandwidth. This is "public underpriced work that degrades block production": the attacker can crowd out normal fee-paying transactions with a stream of free `transfer`/`burn` extrinsics, at a cost far below what an equivalent amount of chain congestion would normally require (ordinary spam requires paying weight fees per transaction; here it's free after setup). On a runtime that includes `pallet-scarcity` with realistic (low) deposit parameters, this degrades fair block-space allocation and can be used to grief block production, matching the impact gate's "public underpriced work that degrades block production or stalls bridge processing" category.

### Likelihood Explanation
High for any runtime that integrates `pallet-scarcity` with its `AsScarcity` extension as documented, since:
- `create_collection`, `define_item`, and `mint` are permissionless, signed-origin dispatchables with no additional ACL beyond paying deposits [3](#0-2) .
- The feeless path only requires *any* NFT, not a privileged/whitelisted one, and the design doc explicitly says "Transfers are feeless when authorized through the `AsScarcity` transaction extension" with no cap on repetition [10](#0-9) .
- No unprivileged/governance/relayer assumption is required — this is purely an unprivileged end-user calling public extrinsics.

### Recommendation
- Do not make `transfer`/`burn` unconditionally feeless for any NFT holder; either rate-limit feeless transfers per instance/time window, require a minimum "rest" interval enforced at dispatch (not just via mempool priority), or charge a reduced-but-nonzero fee instead of `Pays::No`.
- Consider bounding how many feeless dispatches a single purse/instance can generate per era, or track a moving cost that increases with transfer frequency, so the exemption cannot be turned into unlimited free block-weight consumption.
- Re-evaluate whether the `feeless_if` gate should also verify NFT provenance/authorization from a runtime-level trusted issuer (analogous to fixing Vether by gating `addExcluded` behind `DAO.sol`), rather than trusting any self-minted instance.

### Proof of Concept
1. Attacker calls `create_collection()` (signed, pays collection deposit) — becomes collection owner [11](#0-10) .
2. Attacker calls `define_item(collection, [])` then `mint(collection, item, purse_A, [])`, minting an NFT into `purse_A`, which it also controls [8](#0-7) .
3. Attacker signs a transaction from `purse_A` with `AsScarcity::AsNft { instance, state_nonce: 0 }` wrapping `Scarcity::transfer(purse_B)`. `AsScarcity::validate` succeeds because `purse_A` owns the NFT with matching nonce, sets `Origin::Nft`, and the call is dispatched with `Pays::No` due to `feeless_if` [12](#0-11) [13](#0-12) .
4. `state_nonce` becomes 1; attacker immediately signs the reverse transfer `purse_B -> purse_A` with `state_nonce: 1`, again fee-free.
5. Repeat steps 3–4 indefinitely: each iteration is a genuine, weight-consuming `transfer` extrinsic charged `Pays::No`, letting the attacker submit unlimited free transactions to occupy block space after the single initial mint cost.

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

**File:** substrate/frame/scarcity/src/lib.rs (L65-76)
```rust
//! Transfers are feeless when authorized through the [`AsScarcity`](extension::AsScarcity)
//! transaction extension. Their transaction priority is the time since the NFT last moved,
//! capped by the runtime. Moving an NFT consumes it from the old purse key and places it at the
//! new one. Each authorization names the permanent instance and its current state nonce. The state
//! nonce invalidates an authorization whenever that instance moves, including collection-owner
//! force-transfers away from and back to the same purse. Following Coinage's purse model,
//! [`AsScarcity`](extension::AsScarcity) replaces the signed origin before ordinary account checks,
//! so an NFT-only purse does not need a System account. Failed dispatch restores the NFT and
//! temporarily locks the purse key; after the lock expires, the same signed transaction may be
//! submitted again if its NFT state is still current. Callers must sign mortal transactions with
//! an era shorter than [`Config::LockPeriod`] so that retrying is always a fresh signing
//! decision; see the [replay and mortality rules](extension#replay-and-mortality).
```

**File:** substrate/frame/scarcity/src/lib.rs (L541-582)
```rust
		/// Create a collection owned by the signer.
		#[pallet::call_index(0)]
		#[pallet::weight(T::WeightInfo::create_collection())]
		pub fn create_collection(origin: OriginFor<T>) -> DispatchResult {
			let owner = ensure_signed(origin)?;
			Self::do_create_collection(owner).map(|_| ())
		}

		/// Define one immutable item and its shared metadata defaults.
		///
		/// The supplied metadata is inherited by every instance minted from this definition.
		/// Instance-specific values belong on [`Self::mint`] or [`Self::set_instance_metadata`].
		#[pallet::call_index(1)]
		#[pallet::weight(T::WeightInfo::define_item(metadata.len() as u32))]
		pub fn define_item(
			origin: OriginFor<T>,
			collection: CollectionId,
			metadata: Vec<(MetadataKeyOf<T>, MetadataValueOf<T>)>,
		) -> DispatchResult {
			let owner = ensure_signed(origin)?;
			Self::do_define_item(owner, collection, metadata).map(|_| ())
		}

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

**File:** substrate/frame/scarcity/src/lib.rs (L584-605)
```rust
		/// Transfer an NFT held by the `Origin::Nft` purse-key origin.
		#[pallet::call_index(3)]
		#[pallet::weight(T::WeightInfo::transfer())]
		#[pallet::feeless_if(|origin: &OriginFor<T>, _to: &T::AccountId| -> bool {
			Pallet::<T>::is_nft_origin(origin)
		})]
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

**File:** substrate/frame/scarcity/src/lib.rs (L612-616)
```rust
		#[pallet::call_index(4)]
		#[pallet::weight(T::WeightInfo::burn(T::MaxInstanceMetadata::get()))]
		#[pallet::feeless_if(|origin: &OriginFor<T>| -> bool {
			Pallet::<T>::is_nft_origin(origin)
		})]
```

**File:** substrate/frame/scarcity/src/extension.rs (L184-194)
```rust
	fn weight(&self, call: &<T as frame_system::Config>::RuntimeCall) -> Weight {
		if matches!(self.0.as_ref(), Some(AsScarcityInfo::AsNft { .. })) &&
			matches!(
				call.is_sub_type(),
				Some(Call::<T>::transfer { .. }) | Some(Call::<T>::burn {})
			) {
			T::WeightInfo::as_scarcity_pipeline()
		} else {
			Weight::zero()
		}
	}
```

**File:** substrate/frame/scarcity/src/extension.rs (L211-252)
```rust
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
		if let Some(to) = transfer_to {
			// Pre-validate the destination so ordinary user error is rejected at the pool and
			// never reaches dispatch, where a failure triggers the backoff lock. The
			// dispatch-time checks remain for genuine same-block races. Mirrors coinage's
			// `validate_transfer` pattern. Burns have no destination checks.
			if to == &owner {
				return Err(CustomInvalidity::TransferToSelf.into());
			}
			if NftsByOwner::<T>::contains_key(to) {
				return Err(CustomInvalidity::DestinationOccupied.into());
			}
		}
		let priority = now.saturating_sub(nft.last_moved).min(T::MaxTransferPriority::get());
		let validity = ValidTransaction::with_tag_prefix("Scarcity")
			.and_provides((nft.instance, nft.state_nonce))
			.priority(priority)
			.into();
		origin.set_caller_from(Origin::Nft { owner: owner.clone(), nft });
		Ok((
			validity,
			Val::UsingNft { owner, instance: *instance, state_nonce: *state_nonce },
			origin,
		))
	}
```
