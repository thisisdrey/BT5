### Title
`DELEGATECALL` to an address with no deployed contract is treated as success in pallet-revive - (File: `substrate/frame/revive/src/exec.rs`)

### Summary
`Stack::delegate_call` in pallet-revive silently returns `Ok(())` when the target address has no contract deployed on it, mirroring the exact bug class in the report: a delegate/proxy call into an address with no code is treated as a successful call rather than failing. Any Solidity contract compiled for pallet-revive that implements an upgradeable/beacon-style proxy pattern (`delegatecall` to an implementation slot) inherits this behavior and can be driven into the same false-success state the ToB report describes for `BeaconDS`/`AccountFactory`.

### Finding Description
In `run_call` (`substrate/frame/revive/src/vm/evm/instructions/contract.rs:184-251`), the `DELEGATECALL` EVM opcode is routed to `Ext::delegate_call`, implemented in `Stack::delegate_call` (`substrate/frame/revive/src/exec.rs:1972-2009`):

```rust
if let Some(executable) = self.push_frame(
    FrameArgs::Call { dest: account_id, cached_info: Some(contract_info), delegated_call: ... },
    value, call_resources, self.is_read_only(), &input_data,
)? {
    self.run(executable, input_data)
} else {
    // Delegate-calls to non-contract accounts are considered success.
    Ok(())
}
``` [1](#0-0) 

`push_frame` returns `None` when the target has no code/contract info (i.e. it is a plain account or an address that has never been deployed to). Instead of surfacing an error, the caller receives `Ok(())` with an empty `last_frame_output`. Back in `run_call`, `Ok(())` is unconditionally translated into `did_revert() == false` and the stack push of `1` (success) at line 244:
```rust
let return_value = interpreter.ext.last_frame_output();
let did_revert = return_value.did_revert();
interpreter.stack.push(U256::from(!did_revert as u8))
``` [2](#0-1) 

Because `did_revert()` on a `Default::default()` output is `false`, the `DELEGATECALL` opcode reports success to the calling Solidity bytecode exactly as EVM does for calls into empty accounts. This is the same broken invariant as the report: the caller has no way to distinguish "delegatecall executed real logic in `implementation`" from "delegatecall silently no-op'd because `implementation` has no code."

### Impact Explanation
Any beacon/UUPS/transparent-proxy pattern written in Solidity and compiled to PVM/EVM bytecode for pallet-revive relies on `delegatecall` returning success only when the implementation actually executed. If an implementation slot is (temporarily or permanently) pointed at an address with no deployed contract — e.g., a failed/rolled-back deployment, a mis-computed CREATE2 address, or a race between setting the implementation and its deployment being finalized — every call routed through the proxy will "succeed" with empty return data instead of reverting. Downstream logic that checks the boolean success return value (rather than inspecting return data length/content) will treat state-changing operations (e.g., margin/collateral updates, authorization checks encoded in the implementation) as having occurred when nothing ran, leading to accounts or contract state being represented by dead proxies — matching the "broken margin accounts" impact in the original report, but here rooted in the chain's own execution engine rather than a bespoke `BeaconDS`.

### Likelihood Explanation
No privileged actor, validator, relayer, or admin action is required — any user can deploy a proxy contract and any user can point (or accidentally leave pointed) its implementation slot at an undeployed/self-destructed address; the very next call through the proxy exercises this path. This is a core execution semantic reachable via ordinary `call`/`eth_transact` extrinsics into user-deployed Solidity contracts, not a governance or malicious-peer scenario, so it falls inside the accepted impact scope (public dispatch/execution path that produces false success/state acceptance).

### Recommendation
This mirrors upstream EVM/Solidity semantics intentionally (Ethereum's `DELEGATECALL` to an EOA/empty account also returns `1`), so it may be "by design" for EVM-equivalence rather than a runtime defect. However, given the framing of the report, it is worth confirming/documenting this explicitly and considering whether pallet-revive should expose an `extcodesize`-based guard or a lint/warning path for proxy-pattern contracts, and ensuring documentation for Solidity developers targeting pallet-revive calls out this EVM-inherited pitfall so beacon/proxy implementations are written with explicit `extcodesize(implementation) > 0` checks (the standard OpenZeppelin mitigation), matching the report's own short-term recommendation of an `extcodesize` check before accepting/using an implementation address.

### Proof of Concept
1. Deploy a minimal Solidity beacon-proxy-style contract `P` on a pallet-revive-enabled chain whose `fallback()` does:
   ```solidity
   (bool ok, ) = implementation.delegatecall(msg.data);
   require(ok);
   ```
2. Set `implementation` to an address that has never had a contract deployed to it (e.g. `address(0x1234...)` with no code), analogous to a failed/undeployed `Account` implementation in the original report.
3. Call any function on `P`. Trace through `run_call` → `Ext::delegate_call` → `Stack::delegate_call`: `push_frame` returns `None`, `Stack::delegate_call` returns `Ok(())`, and `run_call` in `contract.rs` pushes success (`1`) onto the interpreter stack with empty return data.
4. Observe that `require(ok)` passes and the calling contract proceeds as though the delegated logic executed, even though `implementation` has no code — reproducing the "calls into an address with no contract deployed succeed" condition described in the report. [3](#0-2) [2](#0-1)

### Citations

**File:** substrate/frame/revive/src/exec.rs (L1990-2009)
```rust
		if let Some(executable) = self.push_frame(
			FrameArgs::Call {
				dest: account_id,
				cached_info: Some(contract_info),
				delegated_call: Some(DelegateInfo {
					caller: self.caller().clone(),
					callee: address,
				}),
			},
			value,
			call_resources,
			self.is_read_only(),
			&input_data,
		)? {
			self.run(executable, input_data)
		} else {
			// Delegate-calls to non-contract accounts are considered success.
			Ok(())
		}
	}
```

**File:** substrate/frame/revive/src/vm/evm/instructions/contract.rs (L225-244)
```rust
	match call_result {
		Ok(()) => {
			let mem_start = return_memory_range.start;
			let mem_length = return_memory_range.len();
			let returned_len = interpreter.ext.last_frame_output().data.len();
			let target_len = min(mem_length, returned_len);

			// success or revert
			interpreter
				.ext
				.frame_meter_mut()
				.charge_or_halt(RuntimeCosts::CopyToContract(target_len as u32))?;

			let return_value = interpreter.ext.last_frame_output();
			let return_data = &return_value.data;
			let did_revert = return_value.did_revert();

			// Note: This can't panic because we resized memory with `get_memory_in_and_out_ranges`
			interpreter.memory.set(mem_start, &return_data[..target_len]);
			interpreter.stack.push(U256::from(!did_revert as u8))
```
