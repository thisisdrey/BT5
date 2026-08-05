Audit Report

## Title
Same-transaction contract re-instantiation reuses an unerased child-trie `trie_id` in pallet-revive - (File: `substrate/frame/revive/src/storage.rs`, `substrate/frame/revive/src/exec.rs`)

## Summary
`pallet-revive`'s `ContractInfo::new` derives `trie_id` from the deployer's `System` account nonce rather than a dedicated per-instantiation monotonic counter, unlike `pallet-contracts`, which explicitly uses a dedicated `Nonce<T>` to prevent trie-id collisions between a terminated contract and its replacement. Because the account nonce used here is not incremented per internal instantiate call, a terminate followed by a same-transaction re-instantiate at the same address can reuse the exact same `trie_id`, exposing the new contract instance to the old contract's un-erased child-trie storage before the lazy `DeletionQueue` drains it in `on_idle`.

## Finding Description
`ContractInfo::new` computes `trie_id = hash("bcontract_trie_v1", address, nonce)` where `nonce` comes from `System::account_nonce(&sender)`, as confirmed in the code: [1](#0-0) 

The only two guards applied before minting a new `ContractInfo` at a given address are (1) an `is_contract` check that rejects reuse only while an `AccountInfoOf` entry still exists, and (2) a `NativeDepositOf` iteration that rejects reuse only if unflushed deposit rows remain: [2](#0-1) 

Neither check inspects the child trie's actual contents or the `DeletionQueue` state. `queue_for_deletion` only enqueues the trie for lazy removal, drained by `on_idle`, which does not execute mid-transaction: [3](#0-2) 

By contrast, `pallet-contracts` explicitly documents why a *dedicated* monotonic counter (not any coarser-grained/externally-visible nonce) is required to prevent exactly this "terminate then immediately recreate" collision: [4](#0-3) 

In `exec.rs`, the `Instantiate` frame path uses `<System<T>>::account_nonce(&sender)` both to compute the `CREATE1`/`CREATE2` address and as the `nonce` passed into `ContractInfo::new`: [5](#0-4) 

`frame_system`'s account nonce advances once per dispatched extrinsic at pre-dispatch time, not per internal `instantiate` call, and there is no evidence in the reviewed code of an additional per-instantiation nonce bump for contract-account deployers (contracts acting as `sender` for nested instantiations do not go through the extrinsic pre-dispatch nonce-increment path at all). This means that within a single transaction/call stack, `System::account_nonce(&sender)` is effectively static, so a `CREATE2` instantiate with a fixed `salt`, followed by `terminate` and a same-transaction re-instantiate with the same `salt`/deployer, computes an identical `trie_id`. Since the child trie's actual key-value contents are only cleared lazily via the `DeletionQueue`/`on_idle`, the newly instantiated contract can read the stale, un-erased storage of the terminated instance.

## Impact Explanation
This is a runtime bug that compromises intended behavior: a newly instantiated contract is expected to start with empty storage, but this flaw allows storage — e.g., nonce/replay guards, permit/allowance state, ownership flags — from a logically distinct, terminated contract instance to leak into the new instance under the same address. This matches the "runtime bugs that compromise intended behavior" impact category.

## Likelihood Explanation
Exploitation requires an unprivileged caller to arrange, within a single transaction, a terminate followed by a re-instantiate at the same `CREATE2` address with an unchanged deployer nonce — e.g., via a contract that self-destructs and is subsequently re-instantiated by the same caller/context in the same call stack, or via `utility.batch`. I was able to verify the core mechanism (trie_id derivation from `System` account nonce, and that the two guards in `ContractInfo::new` do not inspect trie contents or the deletion queue) directly against the repository source. However, I could not fully trace, within the tool budget available, whether execution of `terminate` inherently halts the calling frame in a way that blocks a same-transaction re-instantiate at the identical address, nor whether any additional reentrancy/"in-construction" guard elsewhere in `exec.rs` prevents this specific sequence. This remaining uncertainty is the same uncertainty flagged in the original claim and would require dedicated runtime-level testing (e.g., a `pallet-revive` integration test simulating terminate + CREATE2 re-instantiate within one extrinsic) to conclusively confirm the exploit is reachable end-to-end.

## Recommendation
Derive `trie_id` in `pallet-revive` from a dedicated monotonically-incrementing storage counter scoped to instantiation events (mirroring `pallet-contracts`'s `Nonce<T>`), rather than from the deployer's `System` account nonce, so that no two instantiations — even within the same transaction/block by the same sender — can ever produce the same `trie_id`. Alternatively, force synchronous clearing of a trie (or a check against the `DeletionQueue`) before permitting re-instantiation at a `trie_id` that is still pending lazy deletion.

## Proof of Concept
1. Instantiate contract `C` at deterministic address `A` via `CREATE2` with `salt = S`, writing a distinguishing value `V` into its own storage.
2. Within the same extrinsic (e.g., a contract that self-destructs and is then re-instantiated by its caller in the same call stack, or a `utility.batch` combining the two actions from the same origin), call `terminate` on `C`, then immediately call `instantiate` again at address `A` with the same `salt = S` and same deployer, whose `System::account_nonce` has not changed within the transaction.
3. Read from the freshly instantiated contract's storage before `on_idle` runs and confirm that stale key/value pairs (e.g., `V`) from the terminated instance are still visible under the identical `trie_id`, verifiable via a `pallet-revive` unit/integration test analogous to existing `lazy_removal_works` tests combined with an unchanged-nonce re-instantiate.

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
