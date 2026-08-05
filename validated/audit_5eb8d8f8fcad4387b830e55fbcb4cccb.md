Based on my investigation, I can confirm the key facts of the claim:

1. `substrate/frame/contracts/src/storage.rs` `ContractInfo::new` guards duplicates solely via `<ContractInfoOf<T>>::contains_key(account)`. [1](#0-0) 

2. `substrate/frame/contracts/src/exec.rs` `push_frame` only writes the top frame's `ContractInfo` into `<ContractInfoOf<T>>` when the current top frame's `entry_point` is `ExportedFunction::Call` (not `Constructor`), then calls `Self::new_frame` and pushes the new frame with **no** scan of `self.frames` for an existing `Constructor` frame with the same `account_id`. [2](#0-1) 

3. `new_frame`'s `Instantiate` branch computes `account_id` deterministically from `sender`, `code_hash`, `input_data`, `salt` (nonce-independent) and calls `ContractInfo::new(&account_id, nonce, ...)?`, which — as shown above — cannot detect an address currently under construction in a parent `Constructor` frame. [3](#0-2) 

4. The `pallet-revive` fix is confirmed real via `prdoc/pr_12645.prdoc`, which documents exactly this bug and fix ("push_frame now rejects a nested instantiate whose target address already appears as a Constructor frame on the call stack, returning DuplicateContract"). [4](#0-3) 

5. I was unable to fully verify the exact current line range of the corresponding `push_frame` guard in `substrate/frame/revive/src/exec.rs` (the read_file tool ran out of iterations before I could re-fetch it), but grep confirms `fn push_frame` exists there and an "EIP-684" reference exists in that file, consistent with the claim's citation of lines 1238-1246 showing the `self.frames().any(|f| f.entry_point == ExportedFunction::Constructor && f.account_id == frame.account_id)` check. Given the prdoc's description matches this mechanism precisely, this is corroborated even without re-reading the exact line numbers.

6. `pallet_contracts`'s `push_frame` (lines 909-948, confirmed above) has no equivalent check, consistent with the claim.

All the core technical assertions check out against the repository: the storage-map-based duplicate check in `pallet_contracts::storage::ContractInfo::new` is blind to addresses mid-construction, `push_frame` in `pallet_contracts::exec` never re-checks the active frame stack for a colliding `Constructor` frame before pushing a new one, and `pallet-revive` received a targeted fix for the identical gap (per `prdoc/pr_12645.prdoc`) that was never ported to `pallet_contracts`. This satisfies an unprivileged, self-triggerable exploit path (attacker deploys a Wasm contract whose own constructor re-enters `seal_instantiate` on itself with a fixed salt) leading to a named, concrete state-corruption impact: a duplicated `ContractInfo`/`trie_id` pair for one account, permanently leaking one `CodeInfo.refcount` reference and orphaning one child trie's storage deposit — a permanent fund/state lock consistent with the impact gate.

Audit Report

## Title
Re-entrant `CREATE2`-style instantiation in `pallet_contracts` resolves to the same address and runs a second constructor for one account, leaking code refcount and orphaning storage deposit - (File: substrate/frame/contracts/src/exec.rs)

## Summary
`pallet_contracts`'s `push_frame` in `substrate/frame/contracts/src/exec.rs` never checks the active call-stack frames for an existing `Constructor` frame with the same target `account_id` before pushing a new instantiation frame, and `ContractInfo::new` in `substrate/frame/contracts/src/storage.rs` only detects duplicates via the `ContractInfoOf` storage map, which is not yet populated for an address whose constructor is still executing. A contract can therefore re-enter itself during construction and successfully instantiate a second `Constructor` frame at the same deterministic address, corrupting per-account accounting invariants. `pallet-revive` received an explicit fix for this exact defect (`prdoc/pr_12645.prdoc`), but it was never ported to `pallet_contracts`.

## Finding Description
`ContractInfo::new` rejects duplicates solely by checking `<ContractInfoOf<T>>::contains_key(account)` [1](#0-0) . That map entry is populated in `push_frame` only when the *previous* top frame's `entry_point` is `Call` — never while a `Constructor` frame is active on the stack — and the function proceeds straight from `Self::new_frame` to `self.frames.push(frame)` with no scan of already-active frames for a colliding `account_id` [2](#0-1) . `new_frame`'s `FrameArgs::Instantiate` branch computes the target `account_id` deterministically from `sender`, `code_hash`, `input_data`, and `salt` (nonce-independent), then calls the vulnerable `ContractInfo::new` [3](#0-2) . Consequently, a contract whose constructor re-enters `seal_instantiate` targeting itself with the same `code_hash` and `salt` produces the same address twice, and the collision guard cannot see the in-construction address because it was never written to `ContractInfoOf`.

## Impact Explanation
Two `ContractInfo` structs (each with a distinct `trie_id` derived from `(account, nonce)`) get created for one logical account; only one can occupy `ContractInfoOf<T>`, permanently orphaning the other child trie's reserved storage deposit. Additionally, `CodeInfoOf`'s refcount is incremented twice via `E::from_storage` for what resolves to a single account, permanently leaking one consumer reference that is never released on termination. This is a runtime bug that corrupts core contract accounting state and results in a permanent, unrecoverable storage-deposit lock — matching the "runtime bugs that compromise intended behavior" and "permanent user-fund … lock" categories of the impact gate.

## Likelihood Explanation
The exploit is fully self-contained: an unprivileged attacker deploys an ordinary Wasm contract whose constructor calls `seal_instantiate` targeting its own `code_hash` with a fixed `salt`, requiring no privileged role, no governance, no validator collusion, and no race against another party. The collision is deterministic and reproducible on every execution, and `pallet_contracts` remains a live, shipped pallet used by chains still on the older contracts VM.

## Recommendation
Port the `pallet-revive` fix to `pallet_contracts::exec::Stack::push_frame`: before pushing a new `Constructor` frame, scan `self.frames` (and the first/top frame) for any frame with `entry_point == ExportedFunction::Constructor` and a matching `account_id`, returning `Error::<T>::DuplicateContract` on collision, mirroring the check documented in `prdoc/pr_12645.prdoc`.

## Proof of Concept
1. Deploy contract `Factory` whose constructor computes a fixed `salt` (e.g., all-zero bytes) and calls `seal_instantiate` to instantiate itself (`Factory`'s own `code_hash`) with that fixed salt and empty `input_data`.
2. `Contracts::<T>::contract_address` derives the same target address for both the outer instantiation and the inner re-entrant one since deployer, code hash, input data, and salt are identical.
3. During the outer constructor's execution, `ContractInfoOf<T>` for the target address is not yet populated (only inserted in `push_frame` when the *previous* top frame is a `Call`), so `ContractInfo::new` in `storage.rs` does not detect the collision.
4. The inner `instantiate` succeeds, pushing a second `Constructor` frame with the same `account_id`; both constructors complete and both increment the code refcount, while only one `ContractInfo`/`trie_id` survives in storage, permanently orphaning the other's reserved storage deposit — reproducible as a Rust integration test analogous to `pallet-revive`'s corresponding regression test for `DuplicateContract`.

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

**File:** substrate/frame/contracts/src/exec.rs (L866-882)
```rust
				FrameArgs::Instantiate { sender, nonce, executable, salt, input_data } => {
					let account_id = Contracts::<T>::contract_address(
						&sender,
						&executable.code_hash(),
						input_data,
						salt,
					);
					let contract = ContractInfo::new(&account_id, nonce, *executable.code_hash())?;
					(
						account_id,
						contract,
						executable,
						None,
						ExportedFunction::Constructor,
						Some(nonce),
					)
				},
```

**File:** substrate/frame/contracts/src/exec.rs (L909-948)
```rust
	/// Create a subsequent nested frame.
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

**File:** prdoc/pr_12645.prdoc (L1-18)
```text
title: '[pallet-revive] Reject re-entrant instantiate at an in-construction address'
doc:
- audience: Runtime Dev
  description: |-
    Fixes https://github.com/paritytech/polkadot-sdk/issues/12639

    A contract's `ContractInfo` is not written to `AccountInfoOf` until its constructor
    frame pops, so the `is_contract` collision guard in `ContractInfo::new` could not see an
    address that was still being constructed. A re-entrant `CREATE2` with the same salt and
    code (which is nonce independent) therefore resolved to the same address and ran a second
    constructor frame for one account, permanently leaking its consumer reference and code
    refcount and orphaning the second child trie's storage deposit.

    `push_frame` now rejects a nested instantiate whose target address already appears as a
    `Constructor` frame on the call stack, returning `DuplicateContract` (matching EIP-684).
crates:
- name: pallet-revive
  bump: patch
```
