### Title
`pallet-revive` silently returns "success" for `delegatecall`/`call` to a codeless address, reviving the exact EVM contract-existence-check gap in on-chain proxy/library patterns - (File: `substrate/frame/revive/src/exec.rs`)

### Summary
`pallet-revive` — the EVM-compatible smart contract pallet in polkadot-sdk — deliberately reproduces the EVM low-level call quirk described in the external report: `call`/`delegatecall` made to an address that has no code succeed silently instead of failing. This is implemented at the pallet level (not just inherited from a careless dApp), so any contract deployed via `pallet-revive` that relies on delegatecall-based logic (e.g., proxy patterns delegating authorization/validation logic to a "library" implementation contract) inherits exactly the invariant break the ToB report warns about: destruction of the delegate target turns a call meant to execute security-critical code into a silent no-op that still reports success.

### Finding Description
In `Stack::new_frame`, when handling a delegated call, the pallet looks up the callee's contract info and, if none is found, aborts frame construction by returning `Ok(None)` instead of an error: [1](#0-0) 

That `Ok(None)` is treated by the caller as "nothing to execute, but the call itself is not an error" — the outer call still returns success with empty return data, exactly mirroring the Solidity/EVM warning quoted in the source report ("`call`, `delegatecall`, and `staticcall` return true ... if the account called is non-existent").

This is confirmed to be intentional, chain-level behavior (not an oversight limited to a specific contract) by the project's own changelog and tests:
- `prdoc/stable2503/pr_7729.prdoc` explicitly states this PR "allow[s] delegate calls to non-contract accounts", changing prior behavior from returning `CodeNotFound` to instead succeeding with empty output, to match Solidity's `address(0).delegatecall(...)` semantics.
- `prdoc/stable2412/pr_5664.prdoc` similarly documents that a plain `call` to a codeless address is treated as a balance transfer, mirroring EVM.
- The pallet's own tests assert this behavior as correct: `delegate_call_missing_contract` explicitly asserts the outer call still succeeds even when the delegatecall target has no code: [2](#0-1) 

- `delegate_call_non_existant_is_noop` in the PVM integration tests confirms a delegatecall to a non-existent address is a complete no-op that still returns success: [3](#0-2) 

Because `pallet-revive` bakes this EVM quirk directly into the runtime's call-dispatch semantics, any Solidity/EVM-style contract ported onto polkadot-sdk (proxy/implementation split, upgradeable contracts, library-based access-control modifiers implemented via `delegatecall`) inherits the same false-state-acceptance primitive described in the report: if the delegate target becomes codeless — via `terminate`/self-destruct, via being an EOA-mapped fallback account, or via a bad/incorrect address — the delegatecall "succeeds" without ever running the intended logic, and the calling contract has no protocol-level way to distinguish "logic ran and returned success" from "no logic ran but the call reported success," unless it independently performs an `EXTCODESIZE`-equivalent check before every delegatecall (exactly the mitigation the original ToB report calls for and that most ported contracts omit).

### Impact Explanation
Any polkadot-sdk chain using `pallet-revive` for EVM compatibility exposes this call semantics to every deployed contract. A proxy/library pattern that delegates access-control or validation logic and does not itself perform an existence check before delegating will silently skip its intended checks once the delegate target is destroyed or misconfigured, causing security-critical logic (ownership checks, reentrancy guards, balance/authorization validation implemented in a shared library) to be bypassed while the caller still observes a successful transaction. This can lead to unauthorized execution/origin escalation or state transitions that should have reverted proceeding as if validated — matching the "unauthorized execution or origin escalation" and "runtime bugs that compromise intended behavior" impact categories.

### Likelihood Explanation
The precondition — a delegatecall target becoming codeless (self-destructed contract, an address that was never a contract, or an address mapped to a plain fallback account) followed by a delegatecall to it from a contract that relies on the delegated logic's outcome — is realistic for ported EVM/Solidity codebases using proxy or diamond/library patterns, since this is a well-known and previously exploited class of bug in the Solidity ecosystem itself (the very TOB report cited). No privileged actor, validator, or off-chain infrastructure is needed: any account can deploy a contract exhibiting this pattern and any account can trigger it once the target is destroyed/absent, making this fully public-entrypoint reachable.

### Recommendation
- Short term: Document prominently (in `pallet-revive` developer docs and in-repo warnings near `delegate_call`/`call` host functions) that callers must perform an explicit code-existence check (equivalent to `EXTCODESIZE`) before relying on delegatecall/call side effects, and provide a cheap host function for that check if one does not already exist.
- Long term: For internal chain-level constructs that themselves rely on calling into pallet-revive contracts (precompiles, system contracts, or any pallet code invoking `Ext::call`/`Ext::delegate_call` for chain-critical logic), always verify contract existence before the call and treat "no code" as a distinct, explicit outcome rather than folding it into "call succeeded."

### Proof of Concept
1. Deploy `Proxy` contract that `delegatecall`s to `Library` address for its `onlyOwner`-style modifier logic (ported 1:1 from an existing EVM proxy pattern).
2. Deploy `Library` at address `L` and wire `Proxy` to delegatecall into `L`.
3. Trigger `Library`'s self-destruct/terminate path (or simply target an address that was never deployed), leaving `L` codeless in `AccountInfo`.
4. Call `Proxy`'s privileged function, which internally does `L.delegatecall(checkOwner())`.
5. Per `new_frame`'s handling (`substrate/frame/revive/src/exec.rs:1113-1120`) and the behavior verified by `delegate_call_missing_contract`/`delegate_call_non_existant_is_noop`, the delegatecall returns success with empty output instead of reverting; `Proxy` interprets the empty/success result as "check passed" and executes the privileged operation for an unauthorized caller.

### Citations

**File:** substrate/frame/revive/src/exec.rs (L1104-1120)
```rust
				// in case of delegate the executable is not the one at `address`
				let executable = if let Some(delegated_call) = &delegated_call {
					if let Some(precompile) =
						<AllPrecompiles<T>>::get(delegated_call.callee.as_fixed_bytes())
					{
						ExecutableOrPrecompile::Precompile {
							instance: precompile,
							_phantom: Default::default(),
						}
					} else {
						let Some(info) = AccountInfo::<T>::load_contract(&delegated_call.callee)
						else {
							return Ok(None);
						};
						let executable = E::from_storage(info.code_hash, meter)?;
						ExecutableOrPrecompile::Executable(executable)
					}
```

**File:** substrate/frame/revive/src/exec/tests.rs (L422-461)
```rust
#[test]
fn delegate_call_missing_contract() {
	let missing_ch = MockLoader::insert(Call, move |_ctx, _| {
		Ok(ExecReturnValue { flags: ReturnFlags::empty(), data: Vec::new() })
	});

	let delegate_ch = MockLoader::insert(Call, move |ctx, _| {
		ctx.ext.delegate_call(&Default::default(), CHARLIE_ADDR, Vec::new())?;
		Ok(ExecReturnValue { flags: ReturnFlags::empty(), data: Vec::new() })
	});

	ExtBuilder::default().build().execute_with(|| {
		place_contract(&BOB, delegate_ch);
		set_balance(&ALICE, 100);

		let origin = Origin::from_account_id(ALICE);
		let mut meter = TransactionMeter::<Test>::new_from_limits(WEIGHT_LIMIT, 0).unwrap();

		// contract code missing should still succeed to mimic EVM behavior.
		assert_ok!(MockStack::run_call(
			origin.clone(),
			BOB_ADDR,
			&mut meter,
			U256::zero(),
			vec![],
			&ExecConfig::new_substrate_tx(),
		));

		// add missing contract code
		place_contract(&CHARLIE, missing_ch);
		assert_ok!(MockStack::run_call(
			origin,
			BOB_ADDR,
			&mut meter,
			U256::zero(),
			vec![],
			&ExecConfig::new_substrate_tx(),
		));
	});
}
```

**File:** substrate/frame/revive/src/tests/pvm.rs (L774-796)
```rust
#[test]
fn delegate_call_non_existant_is_noop() {
	let (caller_binary, _caller_code_hash) = compile_module("delegate_call_simple").unwrap();

	ExtBuilder::default().existential_deposit(500).build().execute_with(|| {
		let _ = <Test as Config>::Currency::set_balance(&ALICE, 1_000_000);

		// Instantiate the 'caller'
		let Contract { addr: caller_addr, .. } =
			builder::bare_instantiate(Code::Upload(caller_binary))
				.native_value(300_000)
				.build_and_unwrap_contract();

		assert_ok!(
			builder::call(caller_addr)
				.value(1337)
				.data((BOB_ADDR, u64::MAX, u64::MAX).encode())
				.build()
		);

		assert_eq!(get_balance(&BOB_FALLBACK), 0);
	});
}
```
