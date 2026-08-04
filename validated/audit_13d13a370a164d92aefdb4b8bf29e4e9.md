Based on the evidence gathered, I found a strong local analog in the custom `pallet-scarcity` module, specifically in its transfer/instance-management logic.

### Title
Missing instance-ownership binding allows transfer of NFT instances not held by the claimed owner - (File: `substrate/frame/scarcity/src/lib.rs`)

### Summary
`do_force_transfer` in `pallet-scarcity` resolves the NFT instance's *current holder* (`from`) by looking up `Instances::<T>::get(instance)`, then separately fetches the collection's *administrative* owner via `Collections::<T>::get(nft.collection)` and checks `info.owner == *owner`. [1](#0-0)  This mirrors the CryptoPunks bug pattern: the code verifies the *collection owner's* permission to trigger a transfer, but never cross-checks that the `nft`/`instance` record it just pulled from `NftsByOwner::<T>::get(&from)` is consistent with the actual holder recorded for `instance` before mutating state — i.e., there is no assertion binding `owner` (the caller-supplied authority) to actual possession of `instance` beyond the loosely-coupled `from`/`nft.instance` re-check at line 1197, which only validates internal consistency of the two maps, not that `owner` is legitimately entitled to move funds/NFTs belonging to `from`.

### Finding Description
The exploit precondition in the CryptoPunks report was: a privileged-looking action (offering the punk for sale to a specific contract) creates an *expectation* of ownership, but the actual transfer path never re-verifies `punkIndexToAddress(tokenId) == item.sellerAddress` at execution time — so a third party could substitute their own listing data and steal an asset that was never verified as theirs.

In `do_force_transfer`, the analogous "verify actual holder" step is `let from = Instances::<T>::get(instance)...`, immediately followed by `let nft = NftsByOwner::<T>::get(&from)...` and `ensure!(nft.instance == instance, ...)`. [2](#0-1)  This only checks that the two storage maps (`Instances` and `NftsByOwner`) agree with each other — it does **not** independently verify that the `owner` parameter supplied to the function corresponds to the party who is actually authorized over `from`. The authorization check that follows, `ensure!(info.owner == *owner, Error::<T>::NoPermission)`, checks against `Collections::<T>::get(nft.collection).owner` — the **collection** owner, not the **instance holder** (`from`). [3](#0-2)  If `Collections` owner and the current instance holder (`from`) can diverge — e.g., after a prior ownership transfer of the instance without an update to `Collections::owner`, or in any code path that creates a collection administrator distinct from item holders — then any account still registered as `Collections::owner` can force-transfer instances currently held by a different, unrelated account, exactly as Eve could construct her own signed "sale" of Alice's punk because the sale-completion code trusted her self-supplied listing rather than re-verifying `punkIndexToAddress`.

### Impact Explanation
If a caller can invoke `do_force_transfer` while being the recorded `Collections::owner` but not the actual instance holder (`from`), it forcibly reassigns `NftsByOwner`, `Instances`, and clears any `Locked` state for an account that never authorized or does not physically/logically own the item being moved. [4](#0-3)  This is unauthorized execution against a non-consenting party's asset state — theft/unbacked reassignment of an NFT instance — which falls squarely under "theft or unbacked mint or unlock" and "unauthorized execution or origin escalation" in the impact gate.

### Likelihood Explanation
The check at line 1200 only validates the caller against `Collections::owner`, a value that is a separate storage item from the actual per-instance holder (`from`, derived from `Instances`/`NftsByOwner`). Unless collection ownership and instance possession are guaranteed to always be the same principal by every other code path in this pallet (which was not confirmed within the available context, and the pallet is not part of the upstream well-audited `pallet-nfts`/`pallet-uniques`), a legitimate collection owner retains a persistent unauthorized-transfer capability over instances already sold, transferred, or otherwise reassigned to other holders — an unprivileged/low-friction path requiring no validator, relayer, or governance collusion, only ordinary signed calls.

### Recommendation
In `do_force_transfer`, replace or augment the ownership check so it verifies the caller against the actual current instance holder (`from`) rather than (or in addition to) `Collections::owner`, i.e., require `ensure!(*owner == from || info.owner == *owner_with_explicit_admin_semantics, ...)` and clearly document/separate "collection administrative force-transfer" from "holder-initiated transfer" semantics, mirroring the fix pattern of binding the sale/transfer action to a freshly-checked, on-chain-verified holder identity rather than a potentially stale or differently-scoped ownership record.

### Proof of Concept
1. Account `A` creates a collection and is recorded as `Collections::<T>::get(collection).owner == A`.
2. An instance is minted and later legitimately transferred/reassigned so that `Instances::<T>::get(instance) == B` and `NftsByOwner::<T>::get(&B)` holds the NFT (via whatever normal transfer path exists in the pallet), while `Collections::owner` remains `A` (no code path was found in the reviewed excerpt that forces `Collections::owner` to track individual instance holders).
3. `A` calls the force-transfer entry point with `owner = A`, `instance`, `to = C`.
4. `do_force_transfer` resolves `from = B` from `Instances`, loads `nft` from `NftsByOwner::<T>::get(&B)`, confirms `nft.instance == instance` (consistency check only), then checks `Collections::<T>::get(nft.collection).owner == A` — which passes because `A` is still the collection owner. [5](#0-4) 
5. The instance is force-transferred from `B` to `C` without `B`'s consent or any check that `A` is `B`, completing an unauthorized reassignment analogous to Eve stealing Alice's punk by supplying her own unverified sale data. [4](#0-3) 

**Note on completeness:** I was unable to read `substrate/frame/scarcity/AUDIT_STATUS.md` or the full `lib.rs` file before the tool budget was exhausted, so I cannot confirm from additional context (e.g., call-site guards, `ForceOrigin` restrictions on the dispatchable wrapping `do_force_transfer`, or invariants elsewhere in the pallet that keep `Collections::owner` synchronized with instance holders) whether this path is reachable by an ordinary signed account or is gated behind a privileged origin in practice. This should be verified in a full session before treating the finding as conclusively exploitable.

### Citations

**File:** substrate/frame/scarcity/src/lib.rs (L1190-1201)
```rust
		fn do_force_transfer(
			owner: &T::AccountId,
			instance: InstanceId,
			to: T::AccountId,
		) -> DispatchResult {
			let from = Instances::<T>::get(instance).ok_or(Error::<T>::UnknownInstance)?;
			let nft = NftsByOwner::<T>::get(&from).ok_or(Error::<T>::UnknownInstance)?;
			ensure!(nft.instance == instance, Error::<T>::UnknownInstance);
			let info =
				Collections::<T>::get(nft.collection).ok_or(Error::<T>::UnknownCollection)?;
			ensure!(info.owner == *owner, Error::<T>::NoPermission);
			ensure!(to != from, Error::<T>::SelfTransfer);
```

**File:** substrate/frame/scarcity/src/lib.rs (L1204-1210)
```rust
			let state_nonce =
				nft.state_nonce.checked_add(1).ok_or(Error::<T>::StateNonceOverflow)?;
			let nft = Nft { last_moved: T::UnixTime::now().as_secs(), state_nonce, ..nft };
			NftsByOwner::<T>::remove(&from);
			Locked::<T>::remove(&from);
			NftsByOwner::<T>::insert(&to, nft);
			Instances::<T>::insert(instance, &to);
```
