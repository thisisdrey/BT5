### Title
Re-entrant `CREATE2` instantiation in `pallet_contracts` resolves to the same address and runs a second constructor for one account - ([File: substrate/frame/contracts/src/exec.rs])

### Summary
`pallet-revive` was patched (see `prdoc/pr_12645.prdoc`) to reject a re-entrant `instantiate` whose target address collides with an address still under construction, because `ContractInfo::new`'s `is_contract`/`contains_key` guard cannot see an address that is mid-construction (its `ContractInfo` is only written to storage when the constructing frame's *Call*-type parent pops, not when a *Constructor* frame is on the stack). The sibling pallet, `pallet_contracts` (`substrate/frame/contracts/src/exec.rs`), still has the exact same gap: it never received the equivalent guard that `pallet-revive`'s `push_frame` now has.

### Finding Description
`ContractInfo::new` in `substrate/frame/contracts/src/storage.rs` (lines 81-110) only rejects a duplicate contract by checking `<ContractInfoOf<T>>::contains_key(account)`: [1](#0-0) 

That storage map entry, however, is only populated for the *current top frame* when a nested frame is pushed and the top frame is a `Call` (not `Constructor`): [2](#0-1) 

So while a contract's constructor is executing, its `ContractInfo` is *not yet* in `ContractInfoOf`. If that constructor re-enters the same deployer and issues another `instantiate` with the same `code_hash` + `salt` (hence the same deterministic address, since `Contracts::<T>::contract_address` is derived purely from `deploying_address`, `code_hash`, `input_data`, and `salt` — nonce-independent): [3](#0-2) 

then `new_frame`'s call to `ContractInfo::new(&account_id, nonce, ...)` at line 873 sees no existing entry for `account_id` and happily returns `Ok`, allowing `push_frame` (lines 910-948) to push a **second** `Constructor` frame for the *same account_id* onto the call stack: [4](#0-3) 

Unlike `pallet-revive`'s current `push_frame`, which explicitly walks the frame stack and rejects this collision (`self.frames().any(|f| f.entry_point == ExportedFunction::Constructor && f.account_id == frame.account_id)` → `Error::<T>::DuplicateContract`): [5](#0-4) 

`pallet_contracts`'s `push_frame` contains **no such check** — it goes straight from `new_frame` to `self.frames.push(frame)` with no scan of already-active `Constructor` frames for the same `account_id`.

### Impact Explanation
Running two constructor frames for one logical account corrupts core accounting invariants of `pallet_contracts`:
- Two separate `ContractInfo` structs (with two distinct `trie_id`s, as `trie_id = hash(account, nonce)`) get created for the same on-chain account, but only one can ultimately occupy `ContractInfoOf<T>` — the other's child-trie storage deposit becomes orphaned (permanently locked/unaccounted balance under `StorageDepositReserve`/`storage_base_deposit`).
- Code reference counting (`CodeInfoOf` refcount, incremented per instantiation via `E::from_storage`) is incremented twice for what resolves to a single account, permanently leaking a consumer reference that is never released on termination — this is precisely the defect the `pallet-revive` prdoc calls out ("permanently leaking its consumer reference and code refcount and orphaning the second child trie's storage deposit").
- This is a public, unprivileged-user-triggerable state-corruption bug (an attacker only needs to deploy an ordinary Wasm contract whose constructor calls `seal_instantiate` on itself with the same salt), matching the gate's "runtime bugs that compromise intended behavior" and "permanent user-fund … lock" categories — no admin, validator, relayer, or front-running is required.

### Likelihood Explanation
High feasibility: the attacker fully controls the contract code deployed and needs no privileged role, no governance action, no validator collusion, and no race against another party's transaction — the collision is self-inflicted within a single atomic call by the same deployer, deterministically reproducible every time. `pallet_contracts` is still an actively shipped pallet (used by chains still on the older contracts VM), so this path remains live wherever it is included in a runtime.

### Recommendation
Port the `pallet-revive` fix (PR that produced `prdoc/pr_12645.prdoc`) to `pallet_contracts`: in `push_frame`, before pushing a new `Constructor` frame, scan `self.frames` (plus `first_frame`) for any existing frame with `entry_point == ExportedFunction::Constructor` and the same `account_id`, and reject with `Error::<T>::DuplicateContract` if found — matching EIP-684 semantics already restored in `pallet-revive`.

### Proof of Concept
1. Deploy contract `Factory` whose constructor computes `salt = [0u8; N]` and calls `seal_instantiate` to instantiate itself (`Factory`'s own `code_hash`) with that fixed `salt` and empty `input_data`.
2. `contract_address(deploying_address, code_hash, input_data, salt)` in `substrate/frame/contracts/src/exec.rs:866-872` computes the same target address for both the outer instantiation and the inner re-entrant one, since deployer, code hash, input data, and salt are all identical.
3. During the outer constructor's execution, `ContractInfoOf<T>` for the target address is not yet populated (only populated on `push_frame` when the *previous* top frame is a `Call`, per lines 922-931) — so `ContractInfo::new` in `storage.rs:86-88` does not detect the collision.
4. The inner `instantiate` call succeeds, pushing a second `Constructor` frame with the same `account_id`; both constructors run to completion and both increment the code refcount, while only one `ContractInfo`/trie_id ultimately survives in storage, orphaning the other's reserved storage deposit permanently. [3](#0-2) [6](#0-5)

### Citations

**File:** substrate/frame/contracts/src/storage.rs (L81-96)
```rust
	pub fn new(
		account: &AccountIdOf<T>,
		nonce: u64,
		code_hash: CodeHash<T>,
	) -> Result<Self, DispatchError> {
		if <ContractInfoOf<T>>::contains_key(account) {
			return Err(Error::<T>::DuplicateContract.into());
		}

		let trie_id = {
			let buf = (account, nonce).using_encoded(T::Hashing::hash);
			buf.as_ref()
				.to_vec()
				.try_into()
				.expect("Runtime uses a reasonable hash size. Hence sizeof(T::Hash) <= 128; qed")
		};
```

**File:** substrate/frame/contracts/src/exec.rs (L866-873)
```rust
				FrameArgs::Instantiate { sender, nonce, executable, salt, input_data } => {
					let account_id = Contracts::<T>::contract_address(
						&sender,
						&executable.code_hash(),
						input_data,
						salt,
					);
					let contract = ContractInfo::new(&account_id, nonce, *executable.code_hash())?;
```

**File:** substrate/frame/contracts/src/exec.rs (L910-948)
```rust
	fn push_frame(
		&mut self,
		frame_args: FrameArgs<T, E>,
		value_transferred: BalanceOf<T>,
		gas_limit: Weight,
		deposit_limit: BalanceOf<T>,
		read_only: bool,
	) -> Result<E, ExecError> {
		if self.frames.len() == T::CallStack::size() {
			return Err(Error::<T>::MaxCallDepthReached.into());
		}

		// We need to make sure that changes made to the contract info are not discarded.
		// See the `in_memory_changes_not_discarded` test for more information.
		// We do not store on instantiate because we do not allow to call into a contract
		// from its own constructor.
		let frame = self.top_frame();
		if let (CachedContract::Cached(contract), ExportedFunction::Call) =
			(&frame.contract_info, frame.entry_point)
		{
			<ContractInfoOf<T>>::insert(frame.account_id.clone(), contract.clone());
		}

		let frame = top_frame_mut!(self);
		let nested_gas = &mut frame.nested_gas;
		let nested_storage = &mut frame.nested_storage;
		let (frame, executable, _) = Self::new_frame(
			frame_args,
			value_transferred,
			nested_gas,
			gas_limit,
			nested_storage,
			deposit_limit,
			self.determinism,
			read_only,
		)?;
		self.frames.push(frame);
		Ok(executable)
	}
```

**File:** substrate/frame/revive/src/exec.rs (L1238-1246)
```rust
			// EIP-684: an in-construction address is not in `AccountInfoOf` yet, so the
			// `is_contract` guard in `ContractInfo::new` misses this re-entrant collision.
			if frame.entry_point == ExportedFunction::Constructor &&
				self.frames().any(|f| {
					f.entry_point == ExportedFunction::Constructor &&
						f.account_id == frame.account_id
				}) {
				return Err(Error::<T>::DuplicateContract.into());
			}
```
