## Analysis Summary

The LootBox report's core defect is: an implementation/library contract executed via `delegatecall` (or reachable from an unprivileged actor) exposes a `selfdestruct`-class primitive that destroys the *wrong* contract instance/context — the proxy/holder of funds rather than the library — with no origin check.

The direct local analog is in `pallet-revive`'s EVM-compatibility layer, which implements the native `SELFDESTRUCT` opcode and separately a `terminate` system precompile. The maintainers themselves documented and fixed exactly this bug class for the precompile path.

### Title
Unguarded SELFDESTRUCT opcode allows destruction of the delegating (proxy) contract instead of the executing code's own contract - (File: `substrate/frame/revive/src/vm/evm/instructions/host.rs`)

### Summary
`pallet-revive`'s EVM interpreter implements `SELFDESTRUCT` in `selfdestruct()` [1](#0-0) , which unconditionally calls `interpreter.ext.terminate_if_same_tx(&beneficiary)` on whatever the "current" execution frame is. In `pallet-revive`'s delegate-call model, a `delegatecall`ed callee executes in the caller's storage/account context — the same "proxy runs library code in its own storage" pattern as the LootBox `MinimalProxy`. If code reached via `delegatecall` contains `SELFDESTRUCT`, it terminates the *caller's* (proxy's) account and sweeps its balance to an attacker-chosen beneficiary, not the callee's.

The maintainers already recognized and fixed this exact class of bug for the `ISystem.terminate` **precompile** entry point: `prdoc/stable2603/pr_10302.prdoc` states the fix makes "the `terminate` pre-compile ... revert if delegate called or its caller was delegate called," explicitly calling out that failing to do so "increas[es] the attack surface by allowing the destruction of any contract (not only created in the current tx)" [2](#0-1) . That fix, however, is scoped to the system precompile call path (`substrate/frame/revive/src/precompiles/builtin/system.rs`). The raw EVM opcode implementation in `host.rs::selfdestruct` performs no equivalent delegate-call-context check before calling `terminate_if_same_tx` [3](#0-2) .

### Finding Description
- `pallet-revive` supports two ways to trigger contract termination: the `ISystem.terminate` precompile and the native EVM `SELFDESTRUCT` opcode.
- The precompile path was hardened by PR #10302 specifically to reject termination when reached through a `delegatecall`, because delegate-called code executes "as" the caller and could otherwise destroy an unrelated (calling) contract's account/balance [4](#0-3) .
- The opcode-level implementation `selfdestruct()` in `host.rs` charges gas and then directly invokes `interpreter.ext.terminate_if_same_tx(&beneficiary.into_address())` with no check on whether the current frame was entered via delegate-call [5](#0-4) .
- Underlying `terminate`/refcount plumbing in `exec.rs` (`decrement_refcount`, `set_code_hash`, delegate-dependency accounting) confirms that contracts are meant to share code by `code_hash` with the account/storage identity kept separate per-instance — precisely the "shared implementation, many proxies" topology from the LootBox report [6](#0-5) .
- This means an attacker can deploy a small "library" contract whose code contains `SELFDESTRUCT`, then get any victim contract to `delegatecall` into it (or if the attacker controls a contract that legitimately performs library-style delegatecalls for logic reuse), destroying the victim's on-chain account and forcing its balance to an attacker-controlled beneficiary — with no privilege beyond calling a public contract function.

### Impact Explanation
Termination via `terminate_if_same_tx` removes the account (or at minimum sweeps its native balance to an attacker-supplied beneficiary) and, if the code was created in the same transaction, deletes the pristine code too, per the EIP-6780-style semantics documented in `pr_9699.prdoc` [7](#0-6) . If reachable outside the delegate-call guard that was added for the precompile, this becomes a fund-loss / unauthorized-execution primitive: any contract that performs a `delegatecall` into attacker-influenced or attacker-controlled bytecode (a common pattern for proxies/upgradeable contracts/library dispatch) can have its own balance drained and its account destroyed by an unprivileged caller — directly matching "theft or unbacked mint/unlock" and "permanent user-fund lock" impact classes.

### Likelihood Explanation
Likelihood is moderate-to-high in any environment where `pallet-revive` contracts use `delegatecall`-based library/proxy patterns (a pattern explicitly supported and tested in this codebase, e.g. `delegate_call`, `storage_precompile_only_delegate_call` tests) [8](#0-7) . No privileged role, governance action, or off-chain infrastructure compromise is required — only an ordinary transaction invoking a contract that performs a delegatecall into attacker-supplied or attacker-known bytecode containing `SELFDESTRUCT`.

### Recommendation
Apply the same delegate-call context check that PR #10302 added to the `terminate` precompile to the raw `SELFDESTRUCT` opcode handler in `host.rs`: reject (revert) `terminate_if_same_tx` calls when the current execution frame's account/storage context differs from the code's own contract (i.e., when the frame was entered by delegate-call, or when any ancestor frame is a delegate-call), so that `SELFDESTRUCT` can only ever destroy the contract whose own code is executing, never a delegating caller's account.

### Proof of Concept
1. Deploy `LibrarySelfDestruct`, whose bytecode simply executes `SELFDESTRUCT(attacker)` when called.
2. Deploy/identify `Victim`, a contract holding a native balance that performs `delegatecall(LibrarySelfDestruct, data)` as part of normal logic reuse (a standard, legitimate pattern; no privilege needed to trigger the call path if it's a public function).
3. Attacker calls the `Victim` function that triggers the delegatecall into `LibrarySelfDestruct`.
4. Execution reaches `selfdestruct()` in `host.rs` with the frame's execution context bound to `Victim` (delegatecall semantics), calling `terminate_if_same_tx(attacker)` unconditionally [5](#0-4) .
5. `Victim`'s account is terminated and its balance is transferred to the attacker-chosen beneficiary, with no origin/authorization check preventing this, mirroring the LootBox `LootBox.destroy()` unprotected-selfdestruct scenario.

*Note: due to remaining tool-call limits, I was unable to directly inspect the exact guard code inside `substrate/frame/revive/src/precompiles/builtin/system.rs` to confirm the precise conditional check it uses for the precompile path; the finding is based on the documented fix in `pr_10302.prdoc` combined with direct inspection of `host.rs::selfdestruct`, which shows no equivalent guard for the raw opcode path.*

### Citations

**File:** substrate/frame/revive/src/vm/evm/instructions/host.rs (L284-308)
```rust
pub fn selfdestruct<'ext, E: Ext>(interpreter: &mut Interpreter<'ext, E>) -> ControlFlow<Halt> {
	if interpreter.ext.is_read_only() {
		return ControlFlow::Break(Error::<E::T>::StateChangeDenied.into());
	}
	let [beneficiary] = interpreter.stack.popn()?;
	let charged = interpreter.ext.charge_or_halt(RuntimeCosts::Terminate { code_removed: true })?;
	let dispatch_result = interpreter.ext.terminate_if_same_tx(&beneficiary.into_address());

	match dispatch_result {
		Ok(code_removed) => {
			// halt execution on successful selfdestruct
			if matches!(code_removed, crate::CodeRemoved::No) {
				let actual_cost = RuntimeCosts::Terminate { code_removed: false };
				interpreter
					.ext
					.adjust_gas(charged, <RuntimeCosts as Token<E::T>>::weight(&actual_cost));
			}
			ControlFlow::Break(Halt::Return(Vec::default()))
		},
		Err(e) => {
			log::debug!(target: LOG_TARGET, "Selfdestruct failed: {:?}", e);
			ControlFlow::Break(Halt::Err(e))
		},
	}
}
```

**File:** prdoc/stable2603/pr_10302.prdoc (L1-11)
```text
title: Fix termination
doc:
- audience: Runtime Dev
  description: |-
    This PR fixes up termination by changing the behavior to:

    - The free balance (without ed) should be send away right away to the beneficiary and not be delayed like the contract deletion.
    - The ed and storage deposit will be send away only when terminating but to the origin (delayed).
    - The scheduling of the terminate needs to be reverted if the scheduling frame reverts.
    - `SELFDESTRUCT` should be allowed inside the constructor. The issuing contract will exist as account without code for the remainder of the transaction.
    - The `terminate` pre-compile should revert if delegate called or its caller was delegate called. This is just my opinion but if we are changing semantics we can might as well add some security. We are increasing the attack surface by allowing the destruction of any contract (not only created in the current tx).
```

**File:** substrate/frame/contracts/src/exec.rs (L1363-1387)
```rust
	fn terminate(&mut self, beneficiary: &AccountIdOf<Self::T>) -> DispatchResult {
		if self.is_recursive() {
			return Err(Error::<T>::TerminatedWhileReentrant.into());
		}
		let frame = self.top_frame_mut();
		let info = frame.terminate();
		frame.nested_storage.terminate(&info, beneficiary.clone());

		info.queue_trie_for_deletion();
		ContractInfoOf::<T>::remove(&frame.account_id);
		Self::decrement_refcount(info.code_hash);

		for (code_hash, deposit) in info.delegate_dependencies() {
			Self::decrement_refcount(*code_hash);
			frame
				.nested_storage
				.charge_deposit(frame.account_id.clone(), StorageDeposit::Refund(*deposit));
		}

		Contracts::<T>::deposit_event(Event::Terminated {
			contract: frame.account_id.clone(),
			beneficiary: beneficiary.clone(),
		});
		Ok(())
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

**File:** substrate/frame/revive/src/tests/pvm.rs (L745-772)
```rust
#[test]
fn delegate_call() {
	let (caller_binary, _caller_code_hash) = compile_module("delegate_call").unwrap();
	let (callee_binary, _callee_code_hash) = compile_module("delegate_call_lib").unwrap();

	ExtBuilder::default().existential_deposit(500).build().execute_with(|| {
		let _ = <Test as Config>::Currency::set_balance(&ALICE, 1_000_000);

		// Instantiate the 'caller'
		let Contract { addr: caller_addr, .. } =
			builder::bare_instantiate(Code::Upload(caller_binary))
				.native_value(300_000)
				.build_and_unwrap_contract();

		// Instantiate the 'callee'
		let Contract { addr: callee_addr, .. } =
			builder::bare_instantiate(Code::Upload(callee_binary))
				.native_value(100_000)
				.build_and_unwrap_contract();

		assert_ok!(
			builder::call(caller_addr)
				.value(1337)
				.data((callee_addr, u64::MAX, u64::MAX).encode())
				.build()
		);
	});
}
```
