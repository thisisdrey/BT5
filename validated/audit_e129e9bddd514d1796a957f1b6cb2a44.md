## Analysis

The Solidity report's core invariant is: **deleting an outer container does not clear inner/associated data that lives at derived storage locations, so a later re-creation of an entry at the same derived location inherits stale state.**

`pallet-revive`'s child-trie model is a structural analog of a "mapping containing a mapping": a contract's own key-value storage lives in a **child trie** keyed by `trie_id`, and that child trie is *not* deleted synchronously on `terminate` — it is only queued and drained lazily in `on_idle`, exactly like the `pruneQuorums()`/`quorumSignatureSenders` case in the report. [1](#0-0) 

The safety of this lazy-deletion design in `pallet-contracts` (the older sibling pallet) explicitly depends on a **dedicated monotonic `Nonce<T>` counter** used only to derive `trie_id`, precisely to prevent the same collision scenario described in the report ("terminate → immediately recreate → stale trie contents reused"): [2](#0-1) 

`pallet-revive`, however, derives `trie_id` from the **caller's `System` account nonce** instead of a dedicated per-instantiation counter: [3](#0-2) [4](#0-3) 

`frame_system`'s account nonce is incremented once per dispatched extrinsic (pre-dispatch), not per internal `instantiate` call. That means within a single extrinsic (e.g. a `utility.batch`, or a contract that self-destructs and is re-instantiated by the same sender/origin in the same transaction, e.g. via `CREATE2` with a fixed `salt`), `System::account_nonce(&sender)` is **constant for the whole transaction**. `ContractInfo::new` only guards against re-use with an `is_contract` check and a `NativeDepositOf` check — neither of which inspects the child trie itself: [5](#0-4) 

Since `terminate` removes `AccountInfoOf` immediately but only *queues* the trie for lazy deletion (drained in `on_idle`, which does not run mid-transaction), and `trie_id = hash("bcontract_trie_v1", address, nonce)` collides whenever `nonce` is unchanged, a same-transaction sequence of *instantiate → self-destruct → re-instantiate* by the same sender/deployer at the same `CREATE2` address would compute an **identical `trie_id`**, causing the new contract instance to read pre-existing, un-erased storage from the terminated instance before the deletion queue ever runs — the exact "deletion does not delete all values" primitive from the report, applied to reused nonces/permit state/allowances stored in contract storage.

This is worth verifying end-to-end (whether an unprivileged caller can actually reach a same-nonce re-instantiate-at-same-address path, e.g. via a contract that self-destructs and calls `CREATE2` again in its own execution, or via `utility.batch`), since I was not able to fully trace whether any other guard (e.g. `DuplicateContract`/in-construction guard, `PendingDepositCleanup`) blocks all such paths within a single block/transaction. The `PendingDepositCleanup` check only inspects `NativeDepositOf`, not the child trie's actual key-value contents, and I found no other check on the trie/`DeletionQueue` at `ContractInfo::new` time.

### Title
Same-transaction contract re-instantiation reuses an unerased child-trie `trie_id` in pallet-revive - (File: `substrate/frame/revive/src/storage.rs`, `substrate/frame/revive/src/exec.rs`)

### Summary
`pallet-revive` derives a terminated-then-recreated contract's `trie_id` from the deployer's `System` account nonce rather than a dedicated per-instantiation monotonic counter (as `pallet-contracts` uses). Because that nonce is fixed for the duration of an extrinsic, a self-destruct followed by a same-transaction re-instantiate at the same address can produce an identical `trie_id`, exposing the new contract to the old contract's un-erased child-trie storage — the lazy deletion queue never runs mid-transaction.

### Finding Description
`ContractInfo::new` computes `trie_id = hash("bcontract_trie_v1", address, nonce)` where `nonce` is `System::account_nonce(&sender)` (the deployer), not a dedicated always-incrementing instantiation counter. `pallet-contracts` explicitly documents why a dedicated `Nonce<T>` was required instead of relying on any coarser-grained counter: to prevent "terminate, then immediately recreate" from reusing a trie whose contents were only queued (not actually) deleted. `pallet-revive`'s termination path (`queue_for_deletion`) similarly only enqueues the trie for lazy removal in `on_idle`; the actual child-trie kill happens later. If the account nonce used to derive `trie_id` does not change between a terminate and a subsequent instantiate within the same transaction (it only advances once per extrinsic), the new instance's `trie_id` collides with the old one, and reads against the "fresh" contract's storage will return stale key/value pairs left by the destroyed contract, before `process_deletion_queue_batch` (run only from `on_idle`) has cleared them.

### Impact Explanation
This falls under "runtime bugs that compromise intended behavior" and "public underpriced work" adjacent categories: it lets an unprivileged caller cause a freshly instantiated contract to silently inherit pre-existing storage (nonces, permit/allowance state, guard flags, reentrancy locks) from a different logical instance, breaking the fundamental invariant that a newly constructed contract starts with empty storage. Depending on what the contract logic keeps in storage (e.g., a nonce-based replay guard, a "used" bitmap, an owner/allowance mapping), this can enable state confusion, replay of previously "consumed" data, or unauthorized transitions — directly mirroring the reported Solidity bug's consequence.

### Likelihood Explanation
The likelihood requires an unprivileged actor to arrange a same-extrinsic terminate + re-instantiate at the same address with an unchanged sender nonce (e.g., `CREATE2` with a fixed `salt`, or a contract that self-destructs and then calls back into `instantiate` in the same call stack, or a `utility.batch` combining call+redeploy from the same origin). This is a plausible on-chain sequence controllable entirely by the calling account without any privileged, validator, or off-chain-relayer assumptions, but it does depend on precise same-transaction timing that I could not fully confirm is unblocked by other reentrancy/duplicate-contract guards (e.g., the `DuplicateContract`/"in-construction" guard added to reject re-entrant instantiate) — this needs runtime-level testing to conclusively confirm exploitability.

### Recommendation
Derive `trie_id` in `pallet-revive` from a dedicated monotonically-incrementing storage counter scoped to instantiation events (mirroring `pallet-contracts`'s `Nonce<T>`), rather than from the deployer's `System` account nonce, so that no two instantiations — even within the same transaction/block by the same sender — can ever produce the same `trie_id`. Alternatively, force synchronous (non-lazy) child-trie clearing before allowing re-instantiation at a `trie_id` that is still pending in the `DeletionQueue`.

### Proof of Concept
Conceptual reproduction (needs a live Devin session against `pallet-revive`'s test harness to confirm empirically):
1. Instantiate contract `C` at deterministic address `A` via `CREATE2` with `salt = S`, writing a distinguishing value `V` into its own storage.
2. Within the same extrinsic/transaction (e.g. a contract that both self-destructs and re-instantiates itself, or a `utility.batch`), call `terminate` on `C` (removing `AccountInfoOf` but only queuing the trie for lazy deletion), then immediately `instantiate` again at the same address `A` with the same `salt = S` and same deployer/sender (whose `account_nonce` has not changed within this transaction).
3. Read from the freshly instantiated contract's storage before `on_idle` runs; observe that value `V` (or other left-over key/value pairs) from the terminated contract is still visible under the new `trie_id`, confirming the collision and stale-data reuse (analogous to `lazy_removal_works`/`lazy_batch_removal_works` tests showing storage persists until `on_idle`, combined with an unchanged nonce producing an identical `trie_id`).

### Citations

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

**File:** substrate/frame/revive/src/storage.rs (L392-397)
```rust
	/// You must make sure that the contract is also removed when queuing for deletion.
	/// Both the contract's child trie and any [`NativeDepositOf`] entries it held are drained
	/// lazily in `on_idle`.
	pub fn queue_for_deletion(trie_id: TrieId, contract: AccountIdOf<T>) {
		DeletionQueueManager::<T>::load().insert(DeletionQueueItem::new(trie_id, contract));
	}
```

**File:** substrate/frame/contracts/src/lib.rs (L1333-1356)
```rust
	/// This is a **monotonic** counter incremented on contract instantiation.
	///
	/// This is used in order to generate unique trie ids for contracts.
	/// The trie id of a new contract is calculated from hash(account_id, nonce).
	/// The nonce is required because otherwise the following sequence would lead to
	/// a possible collision of storage:
	///
	/// 1. Create a new contract.
	/// 2. Terminate the contract.
	/// 3. Immediately recreate the contract with the same account_id.
	///
	/// This is bad because the contents of a trie are deleted lazily and there might be
	/// storage of the old instantiation still in it when the new contract is created. Please
	/// note that we can't replace the counter by the block number because the sequence above
	/// can happen in the same block. We also can't keep the account counter in memory only
	/// because storage is the only way to communicate across different extrinsics in the
	/// same block.
	///
	/// # Note
	///
	/// Do not use it to determine the number of contracts. It won't be decremented if
	/// a contract is destroyed.
	#[pallet::storage]
	pub(crate) type Nonce<T: Config> = StorageValue<_, u64, ValueQuery>;
```

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
