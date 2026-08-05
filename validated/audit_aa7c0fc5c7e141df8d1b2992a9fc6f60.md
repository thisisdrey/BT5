Audit Report

## Title
`pallet-revive` derives a new contract's `trie_id` from the deployer's resettable `frame_system` account nonce instead of a dedicated monotonic counter, allowing a self-reaped deployer to regenerate an identical `(address, nonce)` pair and inherit undrained child-trie storage from a prior self-destructed contract - (File: substrate/frame/revive/src/exec.rs, substrate/frame/revive/src/storage.rs)

## Summary
`pallet-revive`'s `instantiate` path computes the new contract's address via `address::create1(deployer, nonce)` and its `trie_id` via `ContractInfo::new(&address, nonce, code_hash)`, both keyed off `<System<T>>::account_nonce(&sender)` [1](#0-0)  rather than a dedicated, never-reset pallet-level counter such as the legacy `pallet_contracts::Nonce`. Because `frame_system::Account<T>` entries (including the nonce field) are fully removed when an account is reaped (`providers == 0 && consumers == 0 && sufficients == 0`), a deployer that drains and refunds its own account can cause its nonce to restart, letting a subsequent `instantiate` recompute an address/trie_id pair identical to one used by an earlier, self-destructed contract whose child trie has not yet been drained by the lazy `on_idle` deletion queue.

## Finding Description
I verified the two central code paths cited in the report against the current repository state:

1. `Frame::new` in `exec.rs` derives `account_nonce` from `<System<T>>::account_nonce(&sender)` and uses it both for `address::create1` and, unmodified, as the `nonce` argument passed into `ContractInfo::new` [1](#0-0) .
2. `ContractInfo::new` hashes `("bcontract_trie_v1", address, nonce)` to build `trie_id`, using that same `frame_system` nonce, with no independent monotonic pallet-level counter involved [2](#0-1) .
3. The only anti-collision guard present in `ContractInfo::new` checks `NativeDepositOf::<T>::iter_prefix(&account_id)` for the target contract *address* — it does not check whether a `DeletionQueueManager` entry still references the same `trie_id`/child trie, and it does not account for the deployer's own nonce having been reset via account reaping [3](#0-2) .
4. Contract deletion (`terminate`) is lazy: `queue_for_deletion` only enqueues a `DeletionQueueItem`, and the actual draining of the child trie and any leftover `NativeDepositOf` rows happens later, budget-limited, inside `process_deletion_queue_batch` during `on_idle` [4](#0-3) .

This confirms the exploit mechanism as described: trie-id derivation is tied to a value (`frame_system`'s per-account nonce) that is not guaranteed to be monotonic across the lifetime of an account id, and the one mitigating check in `ContractInfo::new` targets a different scenario (stale deposits for a reused *address*) than the one in this report (deployer nonce reset causing address+trie_id regeneration).

## Impact Explanation
If exploitable, this allows an unprivileged, self-controlled account to cause a newly instantiated contract to inherit pre-existing child-trie storage from an earlier, logically distinct contract instance at the same computed address, bypassing the invariant that constructors execute against empty storage. This matches the "runtime bugs that compromise intended behavior" impact category, since it corrupts an implicit state-integrity guarantee (fresh contract state) without requiring any privileged actor.

## Likelihood Explanation
The precondition — fully reaping and then refunding one's own account — is entirely within an attacker's control and requires no cooperation from other parties. However, I was unable to fully confirm within the available tool-based investigation two important secondary conditions the report itself flags as unverified:
- Whether `frame_system`'s nonce actually resets to a value that recreates an *exact* previous `(deployer, nonce)` pair in the current codebase (I found test evidence — `test_default_account_nonce` — suggesting the default nonce returned after removal may be tied to the current block number rather than a fixed `0`, which changes but does not eliminate the collision feasibility, since block numbers are also predictable/attacker-influenced within limits).
- Whether the attacker can reliably win the race against the weight-budgeted `on_idle` drain of the specific `DeletionQueueManager` entry for the `trie_id` in question, which depends on chain congestion and is not something I could verify statically.

These are feasibility caveats already acknowledged in the report's own "Likelihood Explanation" section, not blocking defects in the identified code pattern itself.

## Recommendation
Reintroduce a dedicated, monotonic, never-decremented pallet-level nonce (mirroring the legacy `pallet_contracts::Nonce`) to seed `trie_id` derivation, decoupled from the deployer's `frame_system` account nonce, so an account-reap-and-refund cycle cannot regenerate a previously used `(address, nonce)` pair. As a complementary or alternative mitigation, extend the check in `ContractInfo::new` to also reject instantiation while any `DeletionQueueManager` entry still references the same `trie_id`, not only stale `NativeDepositOf` rows for the address.

## Proof of Concept
1. Fund a fresh account `D` with exactly the existential deposit and no other providers/consumers/sufficients.
2. `D` calls `instantiate` (nonce `N`) to create contract `C1` at `address0 = create1(D, N)`; write storage via `set_storage`; note `trie_id0` from `ContractInfo::new(&address0, N, code_hash)`.
3. `D` calls `terminate` on `C1` — this removes `AccountInfoOf` immediately but queues `trie_id0` for lazy deletion via `AccountInfo::queue_for_deletion` [5](#0-4) .
4. `D` transfers away its remaining balance so that `frame_system::dec_providers`/`dec_sufficients` fully reaps `D`'s `Account<T>` entry.
5. Before the `on_idle` pass drains the queued `trie_id0` entry, refund `D` and issue a new `instantiate` call from `D` such that the recomputed nonce again equals `N` (or otherwise collides), reproducing `address0`/`trie_id0`.
6. Read storage of the "new" contract at `address0` and confirm it returns the value written in step 2, demonstrating storage bleed across two logically distinct contract instances that should have been isolated.

### Citations

**File:** substrate/frame/revive/src/exec.rs (L1141-1163)
```rust
			FrameArgs::Instantiate { sender, executable, salt, input_data } => {
				let deployer = T::AddressMapper::to_address(&sender);
				let account_nonce = <System<T>>::account_nonce(&sender);
				let address = if let Some(salt) = salt {
					address::create2(&deployer, executable.code(), input_data, salt)
				} else {
					use sp_runtime::Saturating;
					address::create1(
						&deployer,
						// the Nonce from the origin has been incremented pre-dispatch, so we
						// need to subtract 1 to get the nonce at the time of the call.
						if origin_is_caller {
							account_nonce.saturating_sub(1u32.into()).saturated_into()
						} else {
							account_nonce.saturated_into()
						},
					)
				};
				let contract = ContractInfo::new(
					&address,
					<System<T>>::account_nonce(&sender),
					*executable.code_hash(),
				)?;
```

**File:** substrate/frame/revive/src/storage.rs (L196-220)
```rust
	pub fn new(
		address: &H160,
		nonce: T::Nonce,
		code_hash: sp_core::H256,
	) -> Result<Self, DispatchError> {
		if <AccountInfo<T>>::is_contract(address) {
			return Err(Error::<T>::DuplicateContract.into());
		}

		// Reject reuse of an address whose previous occupant still has unflushed
		// `NativeDepositOf` rows in the deletion queue. The on_idle drain will eventually
		// clear them; until it does, instantiating here would let the new contract inherit
		// stale per-payer entitlements.
		let account_id = T::AddressMapper::to_fallback_account_id(address);
		if NativeDepositOf::<T>::iter_prefix(&account_id).next().is_some() {
			return Err(Error::<T>::PendingDepositCleanup.into());
		}

		let trie_id = {
			let buf = ("bcontract_trie_v1", address, nonce).using_encoded(T::Hashing::hash);
			buf.as_ref()
				.to_vec()
				.try_into()
				.expect("Runtime uses a reasonable hash size. Hence sizeof(T::Hash) <= 128; qed")
		};
```

**File:** substrate/frame/revive/src/storage.rs (L392-480)
```rust
	/// You must make sure that the contract is also removed when queuing for deletion.
	/// Both the contract's child trie and any [`NativeDepositOf`] entries it held are drained
	/// lazily in `on_idle`.
	pub fn queue_for_deletion(trie_id: TrieId, contract: AccountIdOf<T>) {
		DeletionQueueManager::<T>::load().insert(DeletionQueueItem::new(trie_id, contract));
	}

	/// Returns the total weight available for deletion-queue processing after subtracting
	/// the fixed [`WeightInfo::deletion_queue_batch`] base.
	pub fn deletion_budget(meter: &WeightMeter) -> Weight {
		meter.limit().saturating_sub(T::WeightInfo::deletion_queue_batch())
	}

	/// Delete as many items from the deletion queue as possible within the supplied weight
	/// limit.
	pub fn process_deletion_queue_batch(meter: &mut WeightMeter) {
		if meter.try_consume(T::WeightInfo::deletion_queue_batch()).is_err() {
			return;
		};

		let mut queue = <DeletionQueueManager<T>>::load();
		if queue.is_empty() {
			return;
		}

		let weight_per_entry = T::WeightInfo::deletion_queue_per_entry()
			.saturating_sub(T::WeightInfo::deletion_queue_batch());
		let weight_per_native_key = T::WeightInfo::deletion_queue_per_native_deposit_key(1)
			.saturating_sub(T::WeightInfo::deletion_queue_per_native_deposit_key(0));
		let weight_per_trie_key = T::WeightInfo::deletion_queue_per_trie_key(1)
			.saturating_sub(T::WeightInfo::deletion_queue_per_trie_key(0));

		let budget = Self::deletion_budget(&meter);
		let mut remaining = budget;

		let key_budget_for = |remaining: Weight, w: Weight| -> u32 {
			// `w == 0` would be a benchmark misconfiguration; refuse to touch keys in that case
			// rather than loop forever.
			remaining.checked_div_per_component(&w).unwrap_or(0).min(u32::MAX as u64) as u32
		};

		loop {
			let Some(entry) = queue.next() else { break };

			// Charge the per-entry overhead.
			let Some(after_entry) = remaining.checked_sub(&weight_per_entry) else { break };
			remaining = after_entry;

			// Phase 1: drain `NativeDepositOf` rows for this contract.
			let key_budget = key_budget_for(remaining, weight_per_native_key);
			if key_budget == 0 {
				break;
			}
			let result =
				NativeDepositOf::<T>::clear_prefix(&entry.value.account_id, key_budget, None);
			remaining = remaining
				.saturating_sub(weight_per_native_key.saturating_mul(u64::from(result.unique)));
			if result.maybe_cursor.is_some() {
				break;
			}

			// Phase 2: kill the child trie.
			let key_budget = key_budget_for(remaining, weight_per_trie_key);
			if key_budget == 0 {
				break;
			}
			#[allow(deprecated)]
			let outcome = child::kill_storage(
				&ChildInfo::new_default(&entry.value.trie_id),
				Some(key_budget),
			);
			match outcome {
				KillStorageResult::SomeRemaining(keys_removed) => {
					remaining = remaining
						.saturating_sub(weight_per_trie_key.saturating_mul(keys_removed.into()));
					break;
				},
				KillStorageResult::AllRemoved(keys_removed) => {
					remaining = remaining.saturating_sub(
						weight_per_trie_key.saturating_mul(u64::from(keys_removed)),
					);
					entry.remove();
				},
			};
		}

		meter.consume(budget.saturating_sub(remaining));
	}

```
