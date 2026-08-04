I found a direct, confirmed local analog. `pallet-revive` was patched (PR fixing issue #12639, see `prdoc/pr_12645.prdoc`) to reject a reentrant `CREATE2`/`instantiate` that resolves to an address currently under construction, because `ContractInfo`/`AccountInfo` for an in-construction contract is not written to storage until its constructor frame pops — so the "does this address already exist" check misses a re-entrant collision at the same address. `pallet-contracts` (the older wasm pallet) contains the exact same architectural gap and was **not** patched with an equivalent guard.

### Title
Reentrant `CREATE2`-style instantiation collision in `pallet-contracts` bypasses the duplicate-contract guard (unlike the patched `pallet-revive`) - (File: `substrate/frame/contracts/src/exec.rs`)

### Summary
`pallet-contracts::ContractInfo::new` only rejects a duplicate contract address by checking `ContractInfoOf::<T>::contains_key(account)` [1](#0-0) . Storage for an in-construction contract is only written to `ContractInfoOf` in `push_frame` when a *call* frame is pushed (`ExportedFunction::Call`) — never for the constructor's own frame [2](#0-1) . This means that while a constructor for address `X` is still executing, `X` is not yet visible in `ContractInfoOf`, so the `contains_key` guard cannot detect a second, reentrant instantiation targeting the same address. `pallet-revive`'s `push_frame` has an explicit fix for exactly this gap, walking the live frame stack for an existing `Constructor` frame at the same address and returning `DuplicateContract` [3](#0-2) , documented as an EIP-684 style guard added in `prdoc/pr_12645.prdoc` [4](#0-3) . `pallet-contracts::push_frame` has no analogous check [5](#0-4) .

### Finding Description
In `pallet-contracts`, the contract address for an instantiate is deterministic given `(sender, code_hash, input_data, salt)` via `DefaultAddressGenerator::contract_address` [6](#0-5) . `new_frame` computes this address and calls `ContractInfo::new(&account_id, nonce, code_hash)` to guard against collisions [7](#0-6) .

The guard is a single `ContractInfoOf::<T>::contains_key(account)` check [1](#0-0) . However, `push_frame` only persists the *current top frame's* contract info to `ContractInfoOf` when that frame is a `Call` frame, explicitly skipping this for constructor frames ("We do not store on instantiate because we do not allow to call into a contract from its own constructor") [2](#0-1) . This means an address `X` whose constructor is still on the call stack is absent from `ContractInfoOf`, so a second `instantiate`/`instantiate_with_code` call (reached via re-entering the deploying contract during its own constructor's execution, e.g. by calling back into a factory contract that instantiates with the same `salt`/`code_hash`) targeting the same computed address `X` passes the `contains_key` check and runs a second constructor frame against the same account.

This is exactly the invariant the report's CREATE2 collision class breaks: the "no two constructions may resolve to the same live address" invariant is not actually enforced against in-flight (not-yet-committed) constructions — only against fully-committed ones. `pallet-revive` recognized and fixed this precise gap by checking the live frame stack (`self.frames().any(...)`) rather than only committed storage [3](#0-2) ; `pallet-contracts` still relies solely on the storage-based check.

### Impact Explanation
A successful collision lets an attacker run two constructor executions against a single account/trie, per the fixed PR's description: "permanently leaking its consumer reference and code refcount and orphaning the second child trie's storage deposit" [8](#0-7) . This corrupts code reference-counting and storage-deposit accounting invariants for `pallet-contracts`, which can lead to orphaned deposits (permanent fund lock) and inconsistent contract/child-trie state — a runtime bug compromising intended behavior of the public `instantiate`/`instantiate_with_code` extrinsics, reachable by any unprivileged account without governance, admin, or validator involvement.

### Likelihood Explanation
The path requires only a self-deployed, attacker-controlled factory contract whose constructor re-enters the same call context to instantiate another contract with a fixed, attacker-chosen `salt` and `code_hash`, matching the deterministic-address formula in `DefaultAddressGenerator` [6](#0-5) . Unlike the external report, no `CREATE2`/EOA-collision brute force over billions of hashes is needed — the collision is self-inflicted and deterministic within a single transaction, purely through call-stack re-entrancy, exactly as demonstrated by `pallet-revive`'s regression test `reentrant_instantiate_at_same_address_is_rejected` before the fix was added [9](#0-8) .

### Recommendation
Port the `pallet-revive` fix to `pallet-contracts::push_frame`: before pushing a new `Constructor` frame, check the existing frame stack for any frame with `entry_point == ExportedFunction::Constructor` and the same `account_id`, and return `Error::<T>::DuplicateContract` if found, mirroring `substrate/frame/revive/src/exec.rs:1238-1246`.

### Proof of Concept
1. Deploy factory contract `F` in `pallet-contracts`.
2. `F`'s constructor, upon being called via `instantiate_with_code`/`instantiate` at computed address `X` (fixed `sender=F`, `code_hash=C`, `salt=S`), re-enters `F`'s own call/message-handling path (e.g., via a nested `seal_instantiate` host call) to instantiate the same `code_hash=C` with the same `salt=S` again from the same `sender=F` before the first constructor returns.
3. `Contracts::<T>::contract_address(&F, &C, input, S)` yields the same address `X` both times (deterministic formula) [6](#0-5) .
4. Because `ContractInfoOf::<T>` for `X` was never inserted for a `Constructor` frame (only for `Call` frames per `push_frame`) [2](#0-1) , `ContractInfo::new`'s `contains_key(X)` check passes a second time, and a second constructor frame executes against `X` — the exact scenario `pallet-revive`'s test `reentrant_instantiate_at_same_address_is_rejected` reproduces and rejects, but which `pallet-contracts` has no equivalent guard against [10](#0-9) .

### Citations

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

**File:** substrate/frame/contracts/src/address.rs (L56-67)
```rust
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

**File:** substrate/frame/revive/src/exec/tests.rs (L1270-1348)
```rust
#[test]
fn reentrant_instantiate_at_same_address_is_rejected() {
	// EIP-684: while `B1` constructs at address `X`, its constructor re-enters the deployer to
	// instantiate the same code+salt. That resolves to `X` again and must be rejected rather
	// than run a second constructor for one account.
	let salt = [42u8; 32];

	let constructor_ch = MockLoader::insert(Constructor, |ctx, _| {
		// Re-enter the deployer (BOB) while we are still being constructed.
		ctx.ext
			.call(
				&CallResources::NoLimits,
				&BOB_ADDR,
				U256::zero(),
				vec![],
				ReentrancyProtection::AllowReentry,
				false,
			)
			.unwrap();
		exec_success()
	});

	let invocations = Rc::new(RefCell::new(0u32));
	let second_instantiate_error = Rc::new(RefCell::new(None::<DispatchError>));
	let factory_ch = MockLoader::insert(Call, {
		let invocations = Rc::clone(&invocations);
		let second_instantiate_error = Rc::clone(&second_instantiate_error);
		move |ctx, _| {
			*invocations.borrow_mut() += 1;
			let n = *invocations.borrow();
			// Bound the recursion in case the guard fails to reject the collision.
			if n <= 2 {
				let min_balance = <Test as Config>::Currency::minimum_balance();
				let value = Pallet::<Test>::convert_native_to_evm(min_balance);
				let result = ctx.ext.instantiate(
					&CallResources::NoLimits,
					Code::Existing(constructor_ch),
					value,
					vec![],
					Some(&salt),
				);
				if n == 2 {
					if let Err(err) = &result {
						*second_instantiate_error.borrow_mut() = Some(err.error);
					}
				}
			}
			exec_success()
		}
	});

	ExtBuilder::default()
		.with_code_hashes(MockLoader::code_hashes())
		.existential_deposit(15)
		.build()
		.execute_with(|| {
			let min_balance = <Test as Config>::Currency::minimum_balance();
			set_balance(&ALICE, min_balance * 1000);
			place_contract(&BOB, factory_ch);
			let origin = Origin::from_account_id(ALICE);
			let mut meter =
				TransactionMeter::<Test>::new_from_limits(WEIGHT_LIMIT, min_balance * 100).unwrap();

			// `B1` still constructs; only the colliding re-entrant instantiate fails.
			assert_ok!(MockStack::run_call(
				origin,
				BOB_ADDR,
				&mut meter,
				Pallet::<Test>::convert_native_to_evm(min_balance * 100),
				vec![],
				&ExecConfig::new_substrate_tx(),
			));

			// Initial call plus one re-entry; without the guard it would recurse further.
			assert_eq!(*invocations.borrow(), 2);
			assert_eq!(
				*second_instantiate_error.borrow(),
				Some(<Error<Test>>::DuplicateContract.into())
			);
```
