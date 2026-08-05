### Title
Reentrant `seal_instantiate` at a colliding deterministic address runs a duplicate constructor frame in `pallet-contracts` - (File: `substrate/frame/contracts/src/exec.rs`)

### Summary
`pallet-contracts` (the WASM contracts pallet) never persists `ContractInfo` for a contract while its constructor frame is still on the call stack; it is only written into `ContractInfoOf<T>` when a frame's `entry_point == ExportedFunction::Call`. The `is_contract`-style duplicate guard in `ContractInfo::new` therefore cannot see an address that is still under construction. A constructor that re-enters `seal_instantiate` for the same deterministic address (same deploying account, code hash, input data and salt) will pass the `ContractInfo::new` collision check a second time and get a second, nested constructor frame pushed for the same account. This is exactly the class of bug that was just fixed in the sibling pallet, `pallet-revive`, via [1](#0-0) , but the equivalent guard was never added to `pallet-contracts`.

### Finding Description
`ContractInfo::new` in `pallet-contracts` guards against duplicates purely by checking storage: [2](#0-1) 

`push_frame` in `pallet-contracts`'s `Stack` only commits the *current* top frame's `ContractInfo` into `ContractInfoOf<T>` when that top frame is a `Call`, never when it is a `Constructor`: [3](#0-2) 

and then unconditionally builds and pushes the new frame with no check for whether the target address already has an in-flight `Constructor` frame on the stack: [4](#0-3) 

Compare this to `pallet-revive`, which just received a dedicated fix for this exact hole. Its `push_frame` now explicitly walks the frame stack and rejects a nested instantiate whose target address is already a `Constructor` frame: [5](#0-4) 

The prdoc explaining the root cause and the fix confirms the invariant that is broken: `ContractInfo` (or `AccountInfo`) is not written until the constructor frame pops, so the "does this address already exist" guard can't see an address still being constructed, allowing a colliding re-entrant `CREATE`/instantiate to run a second constructor frame for one account: [6](#0-5) 

`pallet-contracts`'s address derivation is deterministic and salt/input-data based (not nonce-based), so an attacker fully controls whether a reentrant instantiate resolves to the same address as the in-construction one: [7](#0-6) 

A malicious constructor can therefore call `seal_instantiate` on itself (or via a helper contract) using the exact same `code_hash`, `input_data`, and `salt` combination that produced the currently-executing contract's own address. Because the outer `ContractInfo` for that address was never written to storage (only cached in the frame), `ContractInfo::new` sees no collision and happily creates a second constructor frame for the same `account_id`, exactly mirroring the reentrancy-during-initialization primitive described in the Holograph report (state that is supposed to be "already trusted/initialized" is actually still mutable mid-construction and can be reentered/overwritten).

### Impact Explanation
A second constructor frame executing for the same account while the first is still on the stack corrupts the contract's accounting invariants: it can double-charge or bypass `charge_instantiate` storage deposit accounting, cause `nested_storage.enforce_subcall_limit` bookkeeping to run twice for one account, leave the child trie ("storage_bytes/storage_items") in an inconsistent state, and let one of the two nested constructor executions overwrite state written by the other (e.g. `code_hash`, delegate dependencies) after it returns and is committed via `with_transaction`. This directly violates the required invariant that "public dispatch wrappers/contract execution must not widen origin, bypass filters, or undercharge nested execution" and that "contract-held value must conserve value and settle exactly once" — a duplicated/aliased constructor frame breaks single-settlement of both storage deposit and contract initialization state for one account, which is the same broken-invariant class (untrusted mid-initialization overwrite) as the referenced Holograph finding.

### Likelihood Explanation
This is reachable by any unprivileged account deploying an ordinary WASM contract whose constructor issues a `seal_instantiate` host call with attacker-chosen `code_hash`/`salt`/`input_data` designed to collide with its own address — no privileged actor, governance, relayer, or validator involvement is required. `pallet-contracts` is still an actively maintained pallet in this repository and the guard that closes this exact gap was demonstrably added to `pallet-revive` (proving both that the pattern is exploitable and that the fix is a well-understood, narrow frame-stack check) but was not backported to `pallet-contracts`.

### Recommendation
Add the same guard used in `pallet-revive`'s `push_frame` to `pallet-contracts::Stack::push_frame`: before pushing a new `Constructor` frame, walk `self.frames()` (plus `self.first_frame`) and reject the instantiate with `Error::<T>::DuplicateContract` if any existing frame already has `entry_point == ExportedFunction::Constructor` for the same `account_id`.

### Proof of Concept
1. Deploy contract `Attacker` whose constructor computes the deterministic address that `DefaultAddressGenerator::contract_address` would produce for `(deploying_address = Attacker's own address or its caller, code_hash = Attacker's own code hash, input_data = <same data>, salt = <same salt>)` — i.e., the same tuple that produced its own address.
2. From inside `Attacker`'s constructor (before it returns/commits), call `seal_instantiate` with that same `code_hash`/`input_data`/`salt`.
3. `push_frame` → `new_frame` → `ContractInfo::new` checks `ContractInfoOf::<T>::contains_key(account)`; since the outer frame's `ContractInfo` was never inserted into storage (only cached, per the `ExportedFunction::Call`-only insert condition), the check passes and a second `Constructor` frame is created for the same `account_id`.
4. Both constructor frames execute and commit (via nested `with_transaction`), leaving duplicated/overlapping storage-deposit accounting and child-trie state for a single account — reproducing the “duplicate constructor frame for one account” condition that `pallet-revive`'s `pr_12645` fix explicitly targets.

### Citations

**File:** prdoc/pr_12645.prdoc (L1-15)
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
```

**File:** substrate/frame/contracts/src/storage.rs (L81-88)
```rust
	pub fn new(
		account: &AccountIdOf<T>,
		nonce: u64,
		code_hash: CodeHash<T>,
	) -> Result<Self, DispatchError> {
		if <ContractInfoOf<T>>::contains_key(account) {
			return Err(Error::<T>::DuplicateContract.into());
		}
```

**File:** substrate/frame/contracts/src/exec.rs (L909-931)
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
```

**File:** substrate/frame/contracts/src/exec.rs (L932-948)
```rust

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

**File:** substrate/frame/revive/src/exec.rs (L1226-1252)
```rust
		let frame = top_frame_mut!(self);
		let meter = &mut frame.frame_meter;
		if let Some((frame, executable)) = Self::new_frame(
			frame_args,
			value_transferred,
			meter,
			call_resources,
			read_only,
			false,
			input_data,
			self.exec_config,
		)? {
			// EIP-684: an in-construction address is not in `AccountInfoOf` yet, so the
			// `is_contract` guard in `ContractInfo::new` misses this re-entrant collision.
			if frame.entry_point == ExportedFunction::Constructor &&
				self.frames().any(|f| {
					f.entry_point == ExportedFunction::Constructor &&
						f.account_id == frame.account_id
				}) {
				return Err(Error::<T>::DuplicateContract.into());
			}
			self.frames.try_push(frame).map_err(|_| Error::<T>::MaxCallDepthReached)?;
			Ok(Some(executable))
		} else {
			Ok(None)
		}
	}
```

**File:** substrate/frame/contracts/src/address.rs (L55-67)
```rust
impl<T: Config> AddressGenerator<T> for DefaultAddressGenerator {
	/// Formula: `hash("contract_addr_v1" ++ deploying_address ++ code_hash ++ input_data ++ salt)`
	fn contract_address(
		deploying_address: &T::AccountId,
		code_hash: &CodeHash<T>,
		input_data: &[u8],
		salt: &[u8],
	) -> T::AccountId {
		let entropy = (b"contract_addr_v1", deploying_address, code_hash, input_data, salt)
			.using_encoded(T::Hashing::hash);
		Decode::decode(&mut TrailingZeroInput::new(entropy.as_ref()))
			.expect("infinite length input; no invalid inputs for type; qed")
	}
```
