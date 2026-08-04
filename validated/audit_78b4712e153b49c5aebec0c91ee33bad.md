## Analysis Summary

The AmbireAccount bug's core broken invariant: **an entity that is not the true owner/controller of a contract can force its destruction (and asset drain) through a code path that was designed for legitimate self-termination, because the destruction target is resolved incorrectly (bound to the wrong contract identity).**

The closest local analog is in `pallet-revive`'s termination/self-destruct machinery, specifically the `terminate_caller` function, which lets a called contract terminate its *caller* rather than itself.

### Title
Untrusted callee contract can force-terminate its caller and steal its balance via the `ISystem.terminate` precompile - (File: `substrate/frame/revive/src/exec.rs`)

### Summary
`pallet-revive` exposes a system precompile (`ISystem.terminate`, invoked at `SYSTEM_ADDR`) that a contract can call to self-destruct. Internally this dispatches to `Ext::terminate_caller`, implemented in `Stack::terminate_caller`. Instead of only allowing a contract to terminate itself, this function walks up the call stack and terminates the frame's **parent** — i.e. the contract that made an ordinary `CALL` into the currently executing (callee) contract. [1](#0-0) 

### Finding Description
`terminate_caller` is guarded only by:
- the current frame must not itself be executing via `delegate_call` (`self.top_frame().delegate.is_none()`),
- the parent frame's `entry_point` must be `ExportedFunction::Call` (not `Constructor`),
- the parent frame must not itself be a delegate-call frame.

None of these checks verify that the parent contract *consented* to being terminated by whatever it called, nor that the caller of the precompile has any privilege over the parent. Any contract `B` that is invoked by contract `A` through a normal external `call` (the overwhelmingly common interaction pattern — token hooks, oracle/adapter callbacks, plugin/strategy calls, reentrancy-safe external calls, etc.) can call `SYSTEM_ADDR` with `ISystem.terminate(attacker_beneficiary)`. This resolves to `terminate_caller`, which:
1. Reads `parent.contract_info()` (contract `A`'s `trie_id` and `code_hash`),
2. Transfers **A's entire native balance** to the attacker-supplied `beneficiary` via `Self::transfer(&self.origin, &parent_account_id, &beneficiary, ...)`,
3. Schedules `A` for deletion (`contracts_to_be_destroyed.insert(parent_account_id, TerminateArgs{...})`), which later runs `do_terminate` — decrementing `A`'s code refcount, deleting `A`'s storage trie, and removing `A`'s `AccountInfoOf`/`ImmutableDataOf` entries. [2](#0-1) 

This is functionally identical to the Ambire bug class: destruction of a contract is triggered by an entity that has no legitimate authority over it, purely through a public-facing execution path (`Ext::terminate_caller` / `ISystem.terminate`), with the fix (EIP-6780-style same-tx/self restriction) applied to the `SELFDESTRUCT` opcode path (`terminate_if_same_tx`) but not applied consistently to this "terminate my caller" precompile path. [3](#0-2) 

The corrupted values are: **`A`'s native balance** (redirected to an attacker-controlled beneficiary instead of `A`'s own logic), and **`A`'s `AccountInfoOf`/storage trie/`code_hash` refcount** (deleted/decremented without `A`'s consent), permanently bricking `A` and any state or funds routed through it.

### Impact Explanation
Any contract `A` that performs an ordinary external `call()` into an untrusted or user-supplied contract `B` (a nearly universal DeFi/wallet pattern — token receive hooks, plugin/adapter/strategy invocation, price-feed callbacks, reward distribution to arbitrary recipients) can have its entire native balance stolen and its contract permanently destroyed by `B`. This is a direct instance of "theft or unbacked mint or unlock" and "permanent user-fund ... lock", achieved purely via a public dispatch/precompile entrypoint, with no governance, admin, validator, collator, or leaked-key assumption.

### Likelihood Explanation
Likelihood is high: the attacker only needs to deploy an ordinary contract `B` and get any contract `A` to call it with a plain `CALL` (not delegatecall) — an interaction pattern that is standard, not adversarial-appearing, and requires no privileged relationship, storage manipulation, or race condition. The existing guards (`delegate.is_none()` checks and `entry_point == Call`) only prevent misuse via delegatecall chains; they do nothing to prevent an unrelated callee from reaching up the call stack to destroy its caller.

### Recommendation
Restrict `terminate_caller` so that a contract can only ever terminate **itself** (matching the semantics already enforced for the `SELFDESTRUCT` opcode path via `terminate_if_same_tx`), or require that the parent contract explicitly authorize termination-by-callee (e.g., via an opt-in flag/capability set at the time of the call), rather than allowing any callee invoked via ordinary `CALL` to destroy its caller and redirect its balance.

### Proof of Concept
1. Deploy contract `A` (e.g., a vault/wallet) that performs `B.call(data)` to an address supplied or influenced by an attacker (a normal external-call pattern, e.g. `token.transfer` hook, reward payout to an arbitrary recipient, or an adapter call).
2. Attacker deploys `B` such that, when invoked by `A`, `B` executes:
   ```solidity
   (bool ok, ) = SYSTEM_ADDR.call(
       abi.encodeWithSelector(ISystem.terminate.selector, attackerAddress)
   );
   ```
   matching the `METHOD_PRECOMPILE` path already exercised in the repo's own fixture `substrate/frame/revive/fixtures/contracts/Terminate.sol`. [4](#0-3) 
3. Inside the precompile, `Stack::terminate_caller` resolves `frames_mut().nth(1)` to `A`'s frame (the parent that called `B`), transfers `A`'s full balance to `attackerAddress`, and queues `A` for deletion.
4. At end of the transaction/call-stack, `do_terminate` deletes `A`'s storage trie and account, permanently bricking `A` and any funds or state that were routed through it — with no consent from `A`'s owner and no governance/admin/validator involvement. [5](#0-4)

### Citations

**File:** substrate/frame/revive/src/exec.rs (L1635-1709)
```rust
		if let Some(mut frame) = frame {
			let account_id = &frame.account_id;
			let prev = top_frame_mut!(self);

			// Only weight counter changes are persisted in case of a failure.
			if !persist {
				prev.frame_meter.absorb_weight_meter_only(frame.frame_meter);
				return;
			}

			// Record the storage meter changes of the nested call into the parent meter.
			// If the dropped frame's contract has a contract info we update the deposit
			// counter in its contract info. The load is necessary to pull it from storage in case
			// it was invalidated.
			frame.contract_info.load(account_id);
			let mut contract = frame.contract_info.into_contract();
			prev.frame_meter
				.absorb_all_meters(frame.frame_meter, account_id, contract.as_mut());

			// only on success inherit the created and to be destroyed contracts
			prev.contracts_created.extend(frame.contracts_created);
			prev.contracts_to_be_destroyed.extend(frame.contracts_to_be_destroyed);

			if let Some(contract) = contract {
				// Persist the info and invalidate the first stale cache we find.
				// This triggers a reload from storage on next use. Only the first
				// cache needs to be invalidated because that one will invalidate the next cache
				// when it is popped from the stack.
				AccountInfo::<T>::insert_contract(
					&T::AddressMapper::to_address(account_id),
					contract,
				);
				if let Some(f) = self.frames_mut().find(|f| f.account_id == *account_id) {
					// Bank before invalidating so finalize doesn't apply the diff a second time.
					bank_pending_changes_and_invalidate(f);
				}
			}
		} else {
			if !persist {
				self.transaction_meter
					.absorb_weight_meter_only(mem::take(&mut self.first_frame.frame_meter));
				return;
			}

			let mut contract = self.first_frame.contract_info.as_contract();
			self.transaction_meter.absorb_all_meters(
				mem::take(&mut self.first_frame.frame_meter),
				&self.first_frame.account_id,
				contract.as_deref_mut(),
			);

			if let Some(contract) = contract {
				AccountInfo::<T>::insert_contract(
					&T::AddressMapper::to_address(&self.first_frame.account_id),
					contract.clone(),
				);
			}
			// End of the callstack: destroy scheduled contracts in line with EVM semantics.
			let contracts_created = mem::take(&mut self.first_frame.contracts_created);
			let contracts_to_destroy = mem::take(&mut self.first_frame.contracts_to_be_destroyed);
			for (contract_account, args) in contracts_to_destroy {
				if args.only_if_same_tx && !contracts_created.contains(&contract_account) {
					continue;
				}
				Self::do_terminate(
					&mut self.transaction_meter,
					self.exec_config,
					&contract_account,
					&self.origin,
					&args,
				)
				.ok();
			}
		}
	}
```

**File:** substrate/frame/revive/src/exec.rs (L1792-1849)
```rust
	/// Performs the actual deletion of a contract at the end of a call stack.
	fn do_terminate(
		transaction_meter: &mut TransactionMeter<T>,
		exec_config: &ExecConfig<T>,
		contract_account: &T::AccountId,
		origin: &Origin<T>,
		args: &TerminateArgs<T>,
	) -> Result<(), DispatchError> {
		let contract_address = T::AddressMapper::to_address(contract_account);

		// If root created this contract we need to use the pallet account_id because root has no
		// account.
		let origin: Origin<T> = match origin {
			Origin::Signed(o) => Origin::Signed(o.clone()),
			Origin::Root => Origin::from_account_id(crate::Pallet::<T>::account_id()),
		};

		let mut delete_contract = |trie_id: &TrieId, code_hash: &H256| {
			// deposit needs to be removed as it adds a consumer
			let refund =
				T::Deposit::refund_all(&contract_account, exec_config.funds(origin.account_id()?))?;

			// we added this consumer manually when instantiating
			System::<T>::dec_consumers(&contract_account);

			// ED was minted when the account was brought into existence; burn it now.
			T::Deposit::destroy_contract(contract_account)?;

			// this is needed to:
			// 1) Send any balance that was send to the contract after termination.
			// 2) To fail termination if any locks or holds prevent to completely empty the account.
			let balance = <Contracts<T>>::convert_native_to_evm(<AccountInfo<T>>::total_balance(
				contract_address.into(),
			));
			Self::transfer(
				&origin,
				contract_account,
				&args.beneficiary,
				balance,
				Preservation::Expendable,
				transaction_meter,
				exec_config,
			)?;

			// this deletes the code if refcount drops to zero
			let _code_removed = <CodeInfo<T>>::decrement_refcount(*code_hash)?;

			// delete the contracts data last as its infallible
			ContractInfo::<T>::queue_for_deletion(trie_id.clone(), contract_account.clone());
			AccountInfoOf::<T>::remove(contract_address);
			ImmutableDataOf::<T>::remove(contract_address);

			// the meter needs to discard all deposits interacting with the terminated contract
			// we do this last as we cannot roll this back
			transaction_meter.terminate(contract_account.clone(), refund);

			Ok(())
		};
```

**File:** substrate/frame/revive/src/exec.rs (L2011-2050)
```rust
	fn terminate_if_same_tx(&mut self, beneficiary: &H160) -> Result<CodeRemoved, DispatchError> {
		if_tracing(|tracer| {
			let addr = T::AddressMapper::to_address(self.account_id());
			tracer.terminate(
				addr,
				*beneficiary,
				self.top_frame()
					.frame_meter
					.eth_gas_left()
					.unwrap_or_default()
					.try_into()
					.unwrap_or_default(),
				crate::Pallet::<T>::evm_balance(&addr),
			);
		});
		let frame = top_frame_mut!(self);
		let info = frame.contract_info();
		let trie_id = info.trie_id.clone();
		let code_hash = info.code_hash;
		let contract_address = T::AddressMapper::to_address(&frame.account_id);
		let beneficiary = T::AddressMapper::to_account_id(beneficiary);

		// balance transfer is immediate
		Self::transfer(
			&self.origin,
			&frame.account_id,
			&beneficiary,
			<Contracts<T>>::evm_balance(&contract_address),
			Preservation::Preserve,
			&mut frame.frame_meter,
			self.exec_config,
		)?;

		// schedule for delayed deletion
		let account_id = frame.account_id.clone();
		self.top_frame_mut().contracts_to_be_destroyed.insert(
			account_id,
			TerminateArgs { beneficiary, trie_id, code_hash, only_if_same_tx: true },
		);
		Ok(CodeRemoved::Yes)
```

**File:** substrate/frame/revive/src/exec.rs (L2550-2580)
```rust
	fn terminate_caller(&mut self, beneficiary: &H160) -> Result<(), DispatchError> {
		ensure!(self.top_frame().delegate.is_none(), Error::<T>::PrecompileDelegateDenied);
		let parent = self.frames_mut().nth(1).ok_or_else(|| Error::<T>::ContractNotFound)?;
		ensure!(parent.entry_point == ExportedFunction::Call, Error::<T>::TerminatedInConstructor);
		ensure!(parent.delegate.is_none(), Error::<T>::PrecompileDelegateDenied);

		let info = parent.contract_info();
		let trie_id = info.trie_id.clone();
		let code_hash = info.code_hash;
		let contract_address = T::AddressMapper::to_address(&parent.account_id);
		let beneficiary = T::AddressMapper::to_account_id(beneficiary);

		let parent_account_id = parent.account_id.clone();

		// balance transfer is immediate
		Self::transfer(
			&self.origin,
			&parent_account_id,
			&beneficiary,
			<Contracts<T>>::evm_balance(&contract_address),
			Preservation::Preserve,
			&mut top_frame_mut!(self).frame_meter,
			&self.exec_config,
		)?;

		// schedule for delayed deletion
		let args = TerminateArgs { beneficiary, trie_id, code_hash, only_if_same_tx: false };
		self.top_frame_mut().contracts_to_be_destroyed.insert(parent_account_id, args);

		Ok(())
	}
```

**File:** substrate/frame/revive/fixtures/contracts/Terminate.sol (L40-47)
```text
	function _terminate(uint8 method, address beneficiary) private {
		bytes memory data = abi.encodeWithSelector(ISystem.terminate.selector, beneficiary);
		(bool success, bytes memory returnData) = (false, "");

		if (method == METHOD_DELEGATE_CALL) {
			(success, returnData) = SYSTEM_ADDR.delegatecall(data);
		} else if (method == METHOD_PRECOMPILE) {
			(success, returnData) = SYSTEM_ADDR.call(data);
```
