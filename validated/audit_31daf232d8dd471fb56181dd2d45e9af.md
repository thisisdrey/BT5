## Title
`terminate()`/`SELFDESTRUCT` in `pallet-revive` defers contract deletion to end-of-transaction, letting other calls execute against a "terminated" contract in the same transaction - ([File: substrate/frame/revive/src/exec.rs])

### Summary
`pallet-revive` deliberately implements EVM's EIP-6780/"post-Dencun" semantics for `SELFDESTRUCT`/`terminate`: calling it does not immediately remove the contract's code or storage. It only (a) immediately transfers the current balance to the beneficiary and (b) schedules the account for deletion in `contracts_to_be_destroyed`, with actual deletion (`do_terminate`) deferred until the outer-most call stack frame is popped at the very end of the transaction [1](#0-0) . This is the exact bug class described in the external report: `SELFDESTRUCT`/`terminate` no longer instantly destroys code or storage, so a contract that "cancelled" itself remains callable and stateful for the rest of the transaction.

### Finding Description
`terminate_if_same_tx` performs the balance transfer immediately, then only inserts a `TerminateArgs` entry into `contracts_to_be_destroyed`; deletion happens later via `do_terminate`, invoked only when the call stack unwinds to the first frame [2](#0-1) . Until that point, the contract's code, storage, and `AccountInfo` are all still fully present and reachable.

This is confirmed directly by the test suite: `call_after_terminate_works` builds a contract that calls `terminate()` and then, in the *same transaction*, successfully calls another function (`echo`) on the just-"terminated" contract and gets a correct return value back [3](#0-2) . Likewise `sent_funds_after_terminate_shall_not_be_credited_to_beneficiary_syscall` shows that after `SELFDESTRUCT`-via-syscall is invoked on a contract created in an earlier transaction, `get_contract_checked(&addr).is_some()` remains true - the contract is not destroyed at all in that scenario, and value sent to it afterward stays with the contract rather than going to the beneficiary [4](#0-3) .

This mirrors the original report precisely: `FixedPrice.sol`/`OpenEdition.sol` used `selfdestruct()` as an access-control kill switch to stop `buy()`, assuming the contract would be immediately wiped out; post EIP-4758/6780 that assumption is false and buys can still succeed after "cancel". Any Solidity contract compiled to `pallet-revive` (via `resolc`) that uses `selfdestruct`/`terminate` as a same-transaction guard against further state-changing calls (e.g. "cancel and refuse all further buys/settlements") is vulnerable to the identical class of bug: any call issued to the contract within the same transaction, or before the deferred deletion actually executes, still reads/writes live storage and dispatches normally, because `do_terminate` runs only once, at the very end, and only removes state at that point [5](#0-4) .

### Impact Explanation
Contracts deployed on `pallet-revive` that rely on `SELFDESTRUCT`/`terminate` to instantly and atomically stop further interaction (a common pattern ported from pre-Dencun Ethereum, exactly as in the escher report) will silently keep accepting calls, state changes, and value transfers after "termination" is triggered, within the same transaction and — in the cross-transaction precompile case — potentially indefinitely if the deletion is not scheduled to run (e.g., `only_if_same_tx` gating, or reentrant calls that never let the first frame finish). This can let an unprivileged caller keep interacting with a contract whose owner/logic believed it was destroyed, producing unauthorized state changes, fund transfers to the wrong party, or double-processing (e.g., "buy after cancel"), matching the Medium-severity impact accepted in the original report ("functionality of the protocol is impacted").

### Likelihood Explanation
High for any ported EVM contract that uses `selfdestruct`/`terminate` as a cancellation/kill-switch pattern (a common and previously-safe idiom pre-EIP-6780). No privileged actor, relayer, or governance action is required — a normal user can simply call the contract's other entry points in the same transaction (or in general, before the deferred destruction actually executes) to interact with "terminated" state, exactly as `call_after_terminate_works` demonstrates works today by design [3](#0-2) .

### Recommendation
This is a deliberate EVM-compatibility design choice (tracked/fixed in `prdoc/stable2512/pr_9699.prdoc` and `prdoc/stable2603/pr_10302.prdoc`) matching real EVM chains post-Dencun, so `pallet-revive` itself is behaving as intended for EVM equivalence [6](#0-5) . The residual risk is entirely at the application layer: any migrated/ported Solidity contract using `selfdestruct` as an instant kill switch must be flagged and audited to use an explicit state flag (e.g. `bool cancelled`) checked at the top of every state-changing function instead of relying on `selfdestruct`/`terminate` to halt further calls.

### Proof of Concept
`substrate/frame/revive/src/tests/sol/terminate.rs::call_after_terminate_works` is a working PoC already in-repo: `TerminateCaller.callAfterTerminate` deploys a `Terminate` contract, calls `terminate()` on it, and then calls `echo(value)` on the same "terminated" contract in the same transaction — the call succeeds and returns the correct value, proving the contract remains fully live and callable after "destruction" is triggered [7](#0-6) [3](#0-2) .

### Citations

**File:** substrate/frame/revive/src/exec.rs (L1692-1707)
```rust
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
```

**File:** substrate/frame/revive/src/exec.rs (L1793-1849)
```rust
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

**File:** substrate/frame/revive/src/tests/sol/terminate.rs (L528-601)
```rust
/// This test does *not* create and terminate the Terminate contract in the same transaction.
/// Therefore, the SYSCALL terminate method does not be transferred to beneficiary.
#[test_matrix(
	[FixtureType::Solc, FixtureType::Resolc],
	[FixtureType::Solc, FixtureType::Resolc]
)]
fn sent_funds_after_terminate_shall_not_be_credited_to_beneficiary_syscall(
	caller_type: FixtureType,
	callee_type: FixtureType,
) {
	let (code, _) = compile_module_with_type("Terminate", callee_type).unwrap();
	let (caller_code, _) = compile_module_with_type("TerminateCaller", caller_type).unwrap();
	ExtBuilder::default().build().execute_with(|| {
		let min_balance = Contracts::min_balance();
		let _ = <Test as Config>::Currency::set_balance(&ALICE, 100_000_000_000);

		let Contract { addr, .. } = builder::bare_instantiate(Code::Upload(code))
			.constructor_data(
				Terminate::constructorCall {
					skip: true,
					method: METHOD_PRECOMPILE,
					beneficiary: DJANGO_ADDR.0.into(),
				}
				.abi_encode(),
			)
			.build_and_unwrap_contract();
		let account = <Test as Config>::AddressMapper::to_account_id(&addr);

		assert!(get_contract_checked(&addr).is_some(), "contract does not exist after create");
		assert_eq!(get_balance(&account), min_balance, "unexpected contract balance after create");

		let Contract { addr: caller_addr, .. } =
			builder::bare_instantiate(Code::Upload(caller_code))
				.native_value(125)
				.build_and_unwrap_contract();
		let caller_account = <Test as Config>::AddressMapper::to_account_id(&caller_addr);

		assert_eq!(
			get_balance(&caller_account),
			125 + min_balance,
			"unexpected caller balance before terminate"
		);

		let result = builder::bare_call(caller_addr)
			.data(
				TerminateCaller::sendFundsAfterTerminateCall {
					terminate_addr: addr.0.into(),
					value: alloy_core::primitives::U256::from(123_000_000u64),
					method: METHOD_SYSCALL,
					beneficiary: DJANGO_ADDR.0.into(),
				}
				.abi_encode(),
			)
			.build_and_unwrap_result();

		assert!(
			!result.did_revert(),
			"sendFundsAfterTerminateCall reverted: {}",
			decode_error(&result.data)
		);
		assert!(
			result.data.is_empty(),
			"sendFundsAfterTerminateCall returned unexpected data: {:?}",
			result.data
		);
		assert!(get_contract_checked(&addr).is_some(), "contract does not exist after terminate");
		assert_eq!(get_balance(&DJANGO), 0, "unexpected DJANGO balance after terminate");
		assert_eq!(
			get_balance(&account),
			123 + min_balance,
			"unexpected contract balance after terminate"
		);
	});
}
```

**File:** substrate/frame/revive/src/tests/sol/terminate.rs (L662-700)
```rust
fn call_after_terminate_works(fixture_type: FixtureType, method: u8) {
	let (code, _) = compile_module_with_type("Terminate", fixture_type).unwrap();
	let (caller_code, _) = compile_module_with_type("TerminateCaller", fixture_type).unwrap();
	ExtBuilder::default().build().execute_with(|| {
		let _ = <Test as Config>::Currency::set_balance(&ALICE, 100_000_000_000);
		let expected_value = alloy_core::primitives::U256::from(0xDEADBEEFu64);

		if fixture_type == FixtureType::Resolc {
			// Need to pre-upload code for PVM
			let _ = <Pallet<Test>>::upload_code(
				RuntimeOrigin::signed(ALICE.clone()),
				code.clone(),
				<BalanceOf<Test>>::MAX,
			);
		}
		let Contract { addr: caller_addr, .. } =
			builder::bare_instantiate(Code::Upload(caller_code)).build_and_unwrap_contract();
		let result = builder::bare_call(caller_addr)
			.data(
				TerminateCaller::callAfterTerminateCall { value: expected_value, method }
					.abi_encode(),
			)
			.build_and_unwrap_result();
		assert!(
			!result.did_revert(),
			"callAfterTerminateCall reverted: {}",
			decode_error(&result.data)
		);

		let decoded =
			TerminateCaller::callAfterTerminateCall::abi_decode_returns(&result.data).unwrap();
		let addr = H160::from_slice(decoded._0.as_slice());
		let account = <Test as Config>::AddressMapper::to_account_id(&addr);
		let value = decoded._1;
		assert_eq!(value, expected_value, "unexpected return value from callAfterTerminateCall");
		assert_eq!(get_balance(&account), 0, "unexpected contract balance after terminate");
		assert_eq!(get_balance(&DJANGO), 0, "unexpected DJANGO balance after terminate");
	});
}
```

**File:** prdoc/stable2512/pr_9699.prdoc (L1-9)
```text
title: Rve/revm selfdestruct2
doc:
- audience: Runtime Dev
  description: |-
    fixes https://github.com/paritytech/polkadot-sdk/issues/9621

    Behavior of `terminate` is changed in accordance with EIP-6780 (and EVM in general):
    - `terminate` only deletes the code from storage if it is called in the same transaction the contract was created.
    - `terminate` does not destroy the contract instantly. The contract is registered for destruction, which happens at the end of the transaction.
```

**File:** substrate/frame/revive/fixtures/contracts/TerminateCaller.sol (L49-56)
```text
    function callAfterTerminate(uint value, uint8 method) external returns (address, uint) {
        inner = new Terminate(true, method, payable(address(this)));
        inner.terminate(0, payable(address(this)));
        bytes memory data = abi.encodeWithSelector(inner.echo.selector, value);
        (bool success, bytes memory returnData) = address(inner).call(data);
        require(success, "call after terminate reverted");
        return (address(inner), returnData.length == 32 ? abi.decode(returnData, (uint)) : 0);
    }
```
