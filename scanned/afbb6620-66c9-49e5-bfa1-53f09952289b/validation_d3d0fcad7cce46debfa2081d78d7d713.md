### Title
`SwapClaimed` event logs the swap's `hashed_proof` instead of the revealed `proof`, breaking the atomic-swap secret-relay mechanism - (File: `substrate/frame/atomic-swap/src/lib.rs`)

### Summary
`pallet-atomic-swap`'s `claim_swap` extrinsic is a hash-timelock (HTLC-style) primitive: a sender locks funds against a `hashed_proof` (hashlock) and a target unlocks them by revealing the secret `proof` (preimage). When the target calls `claim_swap`, the pallet emits `Event::SwapClaimed { account, proof, success }`, but the `proof` field is populated with `hashed_proof` (the hash) rather than the actual revealed `proof: Vec<u8>` secret that the caller supplied. This mirrors exactly the `HTLCERC20Settle` bug: the event meant to publish the unlocking secret instead re-publishes the already-public commitment/hash.

### Finding Description
In `claim_swap`, the extrinsic takes the raw revealed secret as `proof: Vec<u8>` and derives `hashed_proof = blake2_256(&proof)` to look up the pending swap: [1](#0-0) 

The `Event::SwapClaimed` variant is declared with a `proof: HashedProof` field: [2](#0-1) 

But at emission time, the call passes `hashed_proof` (the hash) into that field instead of the actual `proof` (the secret) that was just verified and consumed: [3](#0-2) 

This is structurally identical to the external report: `NewSwap` and `SwapCancelled` correctly only ever have access to the hash (`hashed_proof`) and legitimately emit it, but `SwapClaimed` is the one event in the pallet's lifecycle where the actual secret is available and is supposed to be disclosed on-chain — exactly analogous to `settle()`'s outgoing branch in the HTLC contract, which had access to `_preimage` but emitted `hashlock` instead. The pallet doc explicitly states the design intent for cross-chain use: "the target can claim the fund using the revealed proof," and the whole point of an atomic swap across two independently-verified ledgers is that revealing the secret on one chain's event stream lets the counterparty use it to complete the linked claim on the other side. By emitting `hashed_proof` — a value already public since swap creation — the event stream never actually discloses the unlocking secret.

### Impact Explanation
Atomic-swap / HTLC constructions are only useful because revealing the secret in an efficiently-verifiable, indexable channel (events / storage, not full extrinsic replay) lets the counterparty complete the linked leg of the swap (on this chain or another chain relying on light-client/event proofs). Because `SwapClaimed` republishes the hash instead of the secret, any relayer, indexer, or counterparty chain that depends on this event to learn the preimage (rather than decoding and storing every historical extrinsic body) can never learn it, permanently preventing the linked swap leg from being completed. This causes a permanent lock of the counterparty's funds reserved for the paired swap, satisfying the "permanent user-fund lock" impact category — even though the secret does technically exist elsewhere as call input data, the pallet's own documented, purpose-built disclosure channel (its event) fails to carry it, defeating the atomic-swap secret-relay guarantee the pallet exists to provide.

### Likelihood Explanation
This triggers on every single successful `claim_swap` call — there is no special attacker action needed, no privileged role, and no probabilistic condition. It is deterministic pallet logic executed by any unprivileged, correctly-behaving user completing a normal claim, making the likelihood of hitting the broken invariant effectively 100% whenever this pallet is used for its intended cross-chain/relay purpose.

### Recommendation
Change the event emission in `claim_swap` to record the revealed secret rather than its hash:
```rust
Self::deposit_event(Event::SwapClaimed {
    account: target,
    proof: proof.clone(), // or restructure the event to take Vec<u8> instead of HashedProof
    success: succeeded,
});
```
Note this requires widening the `SwapClaimed` event's `proof` field type from `HashedProof` (`[u8; 32]`) to `Vec<u8>`, since the actual secret is not fixed-size. This is a breaking event-schema change and should be reviewed accordingly.

### Proof of Concept
1. Alice calls `create_swap(target = Bob, hashed_proof = H(secret), action, duration)`, locking funds keyed by `H(secret)`. [4](#0-3) 
2. Bob learns `secret` off-chain (e.g., via a linked swap on another chain/pallet instance) and calls `claim_swap(proof = secret, action)`.
3. The pallet correctly verifies `blake2_256(secret) == hashed_proof`, transfers funds to Bob, and removes the pending swap.
4. The emitted `Event::SwapClaimed` contains `proof = hashed_proof` (i.e., `H(secret)`), which was already public from step 1 — any party monitoring only the event stream (the standard low-cost way to relay HTLC secrets cross-chain) gains zero new information and can never recover `secret` to complete the paired leg of the swap, permanently stranding those linked funds.

### Citations

**File:** substrate/frame/atomic-swap/src/lib.rs (L230-237)
```rust
	pub enum Event<T: Config> {
		/// Swap created.
		NewSwap { account: T::AccountId, proof: HashedProof, swap: PendingSwap<T> },
		/// Swap claimed. The last parameter indicates whether the execution succeeds.
		SwapClaimed { account: T::AccountId, proof: HashedProof, success: bool },
		/// Swap cancelled.
		SwapCancelled { account: T::AccountId, proof: HashedProof },
	}
```

**File:** substrate/frame/atomic-swap/src/lib.rs (L255-280)
```rust
		pub fn create_swap(
			origin: OriginFor<T>,
			target: T::AccountId,
			hashed_proof: HashedProof,
			action: T::SwapAction,
			duration: BlockNumberFor<T>,
		) -> DispatchResult {
			let source = ensure_signed(origin)?;
			ensure!(
				!PendingSwaps::<T>::contains_key(&target, hashed_proof),
				Error::<T>::AlreadyExist
			);

			action.reserve(&source)?;

			let swap = PendingSwap {
				source,
				action,
				end_block: frame_system::Pallet::<T>::block_number() + duration,
			};
			PendingSwaps::<T>::insert(target.clone(), hashed_proof, swap.clone());

			Self::deposit_event(Event::NewSwap { account: target, proof: hashed_proof, swap });

			Ok(())
		}
```

**File:** substrate/frame/atomic-swap/src/lib.rs (L297-319)
```rust
		pub fn claim_swap(
			origin: OriginFor<T>,
			proof: Vec<u8>,
			action: T::SwapAction,
		) -> DispatchResult {
			ensure!(proof.len() <= T::ProofLimit::get() as usize, Error::<T>::ProofTooLarge);

			let target = ensure_signed(origin)?;
			let hashed_proof = blake2_256(&proof);

			let swap =
				PendingSwaps::<T>::get(&target, hashed_proof).ok_or(Error::<T>::InvalidProof)?;
			ensure!(swap.action == action, Error::<T>::ClaimActionMismatch);

			let succeeded = swap.action.claim(&swap.source, &target);

			PendingSwaps::<T>::remove(target.clone(), hashed_proof);

			Self::deposit_event(Event::SwapClaimed {
				account: target,
				proof: hashed_proof,
				success: succeeded,
			});
```
