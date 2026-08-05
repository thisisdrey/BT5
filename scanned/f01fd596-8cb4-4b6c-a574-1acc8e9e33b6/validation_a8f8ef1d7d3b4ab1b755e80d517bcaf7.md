### Title
Reentrant CREATE2 during constructor execution lets an attacker run two constructor frames for one account in `pallet-contracts` - (File: `substrate/frame/contracts/src/exec.rs`)

### Summary
The Holograph bug is a reentrancy-during-initialization flaw: the trusted "initialized" state is only committed after a delegatecall into arbitrary code completes, so a re-entrant call during construction can run the initializer a second time and stomp on trusted slots before the first pass finishes. `pallet-revive` had the exact same class of bug — `ContractInfo` is only written into `AccountInfoOf` after the constructor frame pops, so a re-entrant `CREATE2` with the same code+salt (address derivation is nonce-independent for `create2`) resolves to the same address and is allowed to run a second constructor frame. This was found and fixed for `pallet-revive` in `prdoc/pr_12645.prdoc` / `substrate/frame/revive/src/exec.rs`. The legacy `pallet-contracts` pallet contains the structurally identical `push_frame`/`new_frame`/`ContractInfo::new` flow but does **not** carry the equivalent guard.

### Finding Description
In `pallet-contracts`, address derivation and account creation happen in `Stack::new_frame` (`substrate/frame/contracts/src/exec.rs:866-882`): for `FrameArgs::Instantiate`, `Contracts::<T>::contract_address(...)` computes the deterministic contract address and `ContractInfo::new(&account_id, nonce, code_hash)` is constructed in memory, but this in-memory `ContractInfo` is *not* persisted to `ContractInfoOf` until the frame's `push_frame` caller commits it — and that only happens for `ExportedFunction::Call` frames, never for constructor frames still in progress (`substrate/frame/contracts/src/exec.rs:922-931`):

```
// We do not store on instantiate because we do not allow to call into a contract
// from its own constructor.
let frame = self.top_frame();
if let (CachedContract::Cached(contract), ExportedFunction::Call) = ...
```

Because a constructor's `ContractInfo` is never written to storage while it is still executing, the `is_contract`/collision guard inside `ContractInfo::new` (which consults on-chain state) cannot see that the target address is "in construction." If a constructor re-enters the top-level dispatcher (e.g. via a delegate call back into a factory, or the constructor itself calling `instantiate` on the same code+salt), `push_frame` in `substrate/frame/contracts/src/exec.rs:909-948` happily creates and pushes a second `Frame` with `entry_point == Constructor` for the *same account address*, running a second constructor for one account. Compare this to the already-patched `pallet-revive` version, which explicitly checks the in-memory call stack rather than only on-chain state:

```
// substrate/frame/revive/src/exec.rs:1238-1246
if frame.entry_point == ExportedFunction::Constructor &&
    self.frames().any(|f| {
        f.entry_point == ExportedFunction::Constructor &&
            f.account_id == frame.account_id
    }) {
    return Err(Error::<T>::DuplicateContract.into());
}
```

`pallet-contracts`'s `push_frame` at `substrate/frame/contracts/src/exec.rs:909-948` contains no such call-stack scan; its only depth check is `MaxCallDepthReached`. As `prdoc/pr_12645.prdoc` states, the consequence of allowing a second constructor frame for one account is: "permanently leaking its consumer reference and code refcount and orphaning the second child trie's storage deposit."

### Impact Explanation
This maps directly to the required "permanent user-fund or bridge-state lock" / "duplicate settlement" impact category. Two constructor executions for the same account:
- Double-increment consumer references and code refcounts that are never fully unwound, permanently pinning storage/deposit state.
- Orphan the second child trie's storage deposit, since only one `ContractInfo` slot exists on-chain for the address, so deposits collected in the second constructor pass are unrecoverable.
- Allow an attacker-authored constructor to overwrite the effective state the first constructor believed was final (analogous to Holograph's holograph-slot overwrite), since the second constructor pass runs with a fresh, uncommitted `ContractInfo`, effectively controlling what gets persisted after the outer frame unwinds.

This is a public, unprivileged entrypoint bug — any account can deploy an arbitrary contract that re-enters `instantiate`/`call` during its own constructor via `pallet_contracts::Pallet::instantiate`/`instantiate_with_code`, no admin/governance/relayer assumption needed.

### Likelihood Explanation
Likelihood is high for any code deployed through `pallet-contracts`: the attacker fully controls the constructor logic and can trivially trigger the re-entrant call pattern (call back into a factory dispatcher, or `seal_instantiate`/cross-contract call with the same code hash and salt) within a single constructor execution, exactly as demonstrated by the `reentrant_instantiate_at_same_address_is_rejected` regression test that was added for `pallet-revive` (`substrate/frame/revive/src/exec/tests.rs:1270-1350`) after the fix — the same PoC pattern applies unmodified to `pallet-contracts`, which lacks the guard.

### Recommendation
Port the fix from `prdoc/pr_12645.prdoc` to `pallet-contracts`: in `Stack::push_frame` (`substrate/frame/contracts/src/exec.rs:909-948`), before pushing a new `Constructor` frame, scan `self.frames()` (plus `self.first_frame`) for an existing frame with `entry_point == ExportedFunction::Constructor` and the same `account_id`, and reject with a `DuplicateContract`-equivalent error if found — matching EIP-684 semantics already restored in `pallet-revive`.

### Proof of Concept
Adapt the existing `pallet-revive` regression test to `pallet-contracts`:
1. Deploy a "factory" contract `Call` handler that, given a fixed `code_hash` and `salt`, calls `seal_instantiate` twice with identical parameters.
2. Deploy a "constructor" contract whose `Constructor` entry point re-enters the factory (`ctx.ext.call(factory_addr, ...)`) before returning.
3. Trigger the factory once from an external account; the first `instantiate` call pushes a `Constructor` frame for address `X` and, inside it, re-enters the factory which issues the second `instantiate` for the same `code_hash`+`salt`, again resolving to `X`.
4. Because `pallet-contracts`'s `push_frame` has no call-stack collision guard (unlike the patched `substrate/frame/revive/src/exec.rs:1238-1246`), the second `instantiate` succeeds instead of returning `DuplicateContract`, running two constructor frames for account `X` and leaving `ContractInfoOf` with double-counted consumer refs/orphaned deposit accounting for the discarded child trie. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

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

**File:** substrate/frame/revive/src/exec.rs (L1191-1252)
```rust
	/// Create a subsequent nested frame.
	fn push_frame(
		&mut self,
		frame_args: FrameArgs<T, E>,
		value_transferred: U256,
		call_resources: &CallResources<T>,
		read_only: bool,
		input_data: &[u8],
	) -> Result<Option<ExecutableOrPrecompile<T, E, Self>>, ExecError> {
		if self.frames.len() as u32 == limits::CALL_STACK_DEPTH {
			return Err(Error::<T>::MaxCallDepthReached.into());
		}

		// We need to make sure that changes made to the contract info are not discarded.
		// See the `in_memory_changes_not_discarded` test for more information.
		// We do not store on instantiate because we do not allow to call into a contract
		// from its own constructor.
		//
		// Additionally, we need to apply pending storage changes to the ContractInfo before
		// saving it, so that child frames can correctly calculate storage deposit refunds.
		// See: <https://github.com/paritytech/contract-issues/issues/213>
		let frame = self.top_frame();
		if let (CachedContract::Cached(contract), ExportedFunction::Call) =
			(&frame.contract_info, frame.entry_point)
		{
			let mut contract_with_pending_changes = contract.clone();
			frame
				.frame_meter
				.apply_pending_storage_changes(&mut contract_with_pending_changes);
			AccountInfo::<T>::insert_contract(
				&T::AddressMapper::to_address(&frame.account_id),
				contract_with_pending_changes,
			);
		}

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

**File:** substrate/frame/revive/src/exec/tests.rs (L1270-1350)
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
		});
}
```

**File:** substrate/frame/contracts/src/exec.rs (L838-948)
```rust
	fn new_frame<S: storage::meter::State + Default + Debug>(
		frame_args: FrameArgs<T, E>,
		value_transferred: BalanceOf<T>,
		gas_meter: &mut GasMeter<T>,
		gas_limit: Weight,
		storage_meter: &mut storage::meter::GenericMeter<T, S>,
		deposit_limit: BalanceOf<T>,
		determinism: Determinism,
		read_only: bool,
	) -> Result<(Frame<T>, E, Option<u64>), ExecError> {
		let (account_id, contract_info, executable, delegate_caller, entry_point, nonce) =
			match frame_args {
				FrameArgs::Call { dest, cached_info, delegated_call } => {
					let contract = if let Some(contract) = cached_info {
						contract
					} else {
						<ContractInfoOf<T>>::get(&dest).ok_or(<Error<T>>::ContractNotFound)?
					};

					let (executable, delegate_caller) =
						if let Some(DelegatedCall { executable, caller }) = delegated_call {
							(executable, Some(caller))
						} else {
							(E::from_storage(contract.code_hash, gas_meter)?, None)
						};

					(dest, contract, executable, delegate_caller, ExportedFunction::Call, None)
				},
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
			};

		// `Relaxed` will only be ever set in case of off-chain execution.
		// Instantiations are never allowed even when executing off-chain.
		if !(executable.is_deterministic() ||
			(matches!(determinism, Determinism::Relaxed) &&
				matches!(entry_point, ExportedFunction::Call)))
		{
			return Err(Error::<T>::Indeterministic.into());
		}

		let frame = Frame {
			delegate_caller,
			value_transferred,
			contract_info: CachedContract::Cached(contract_info),
			account_id,
			entry_point,
			nested_gas: gas_meter.nested(gas_limit),
			nested_storage: storage_meter.nested(deposit_limit),
			allows_reentry: true,
			read_only,
		};

		Ok((frame, executable, nonce))
	}

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
