### Title
Unmetered memory expansion in EVM `RETURN`/`REVERT` opcodes allows underpriced DoS - (File: `substrate/frame/revive/src/vm/evm/instructions/control.rs`)

### Summary
The external report's core primitive is: an untrusted callee returns an oversized byte array that forces the caller to do unbounded, unpriced work (memory expansion) before any size check can reject it. In `pallet-revive`'s EVM interpreter, the `RETURN`/`REVERT` opcode handler `return_inner` performs `interpreter.memory.resize(offset, len)` and materializes the full output buffer with **no gas charge at all**, unlike every other memory-touching instruction in the same interpreter (e.g. `calldatacopy`, `returndatacopy`) which route through `memory_resize()` and explicitly `charge_or_halt(RuntimeCosts::CopyToContract(len))` before touching memory.

### Finding Description
`return_inner` in `substrate/frame/revive/src/vm/evm/instructions/control.rs` is shared by the `RET` and `REVERT` opcodes: [1](#0-0) 

Compare with the sibling `RETURNDATACOPY`/`CALLDATACOPY` path which always charges before resizing memory: [2](#0-1) 

`return_inner` calls `interpreter.memory.resize(offset, len)` and then `interpreter.memory.slice_len(offset, len).to_vec()` directly, with **no** `interpreter.ext.charge_or_halt(...)` call for the memory growth or the copy — the only gas accounting for `RETURN`/`REVERT` in `mod.rs`'s opcode dispatch table is whatever base cost (if any) is charged elsewhere; the size-dependent cost that every other memory op pays is absent here.

The only place that bounds the resulting buffer is the caller-side check in `Contract::run`, which happens **after** the callee has already fully executed and materialized `output.data`: [3](#0-2) 

This is exactly the return-bomb pattern from the report: the check that is supposed to prevent the DoS (`ReturnDataTooLarge`) fires only after the expensive, unmetered work (memory resize + `to_vec()` copy) has already been performed. The `len` value comes straight off the EVM stack under full attacker control (any value up to `usize::MAX` after `as_usize_or_halt` conversion), and `memory.resize`/`slice_len` allocate/copy that much memory unconditionally.

### Impact Explanation
Because the memory growth for `RETURN`/`REVERT` is not weight-metered, a contract can request an arbitrarily large `len` (e.g. `revert(0, huge_len)` semantics equivalent to the POC in the report, or an equally large `RETURN`) and force the runtime to allocate and copy that many bytes in native memory during block execution, before `exec.rs` gets a chance to reject it with `ReturnDataTooLarge`. This is unpriced, attacker-controlled resource consumption inside consensus-critical execution:
- It degrades or stalls block production (excessive allocation/copy cost not reflected in charged weight), matching the "public underpriced work that degrades block production" impact category.
- As in the original report, it can also be used to make a specific call path (e.g. one leg of a conditional contract, or one action among several dispatched from an untrusted callee) permanently fail/OOM while leaving another path executable, causing one-sided or DoS'ed execution.

### Likelihood Explanation
Any unprivileged user can deploy an EVM contract on `pallet-revive` and have any other account (or another contract, including one it does not control) call into it; no privileged, governance, relayer, or validator role is required. The vulnerable code path (`RET`/`REVERT`) is reachable on every ordinary EVM transaction that hits these opcodes, and the attacker fully controls `len` via the stack value passed to `RETURN`/`REVERT`. The only mitigation in place (`ReturnDataTooLarge` in `exec.rs`) is a post-hoc check that does not prevent the underlying unmetered work from being performed first.

### Recommendation
Charge gas for the memory resize and copy performed in `return_inner`, mirroring the `memory_resize()` helper used by `calldatacopy`/`returndatacopy` (i.e. call `interpreter.ext.charge_or_halt(RuntimeCosts::CopyToContract(len as u32))` before `interpreter.memory.resize`/`slice_len`), and/or clamp `len` against `limits::CALLDATA_BYTES` at the point of the `RETURN`/`REVERT` opcode itself rather than only after the callee has already run to completion.

### Proof of Concept
1. Deploy an EVM contract on `pallet-revive` whose bytecode, on some branch, executes `REVERT(0, 100_000_000)` (or `RETURN` with an equally large length) — analogous to the report's `assembly { revert(0, 1_000_000) }`.
2. Call this contract from any other account/contract.
3. Execution reaches `return_inner` in `substrate/frame/revive/src/vm/evm/instructions/control.rs`, which resizes interpreter memory to the attacker-chosen `len` and copies it into a `Vec<u8>` with zero gas charge for that specific operation.
4. Only afterward does `Contract::run` in `substrate/frame/revive/src/exec.rs` (lines 1420-1428) check `output.data.len() > limits::CALLDATA_BYTES` and reject with `ReturnDataTooLarge` — by which point the unmetered allocation/copy has already occurred, reproducing the return-bomb style underpriced work described in the original report.

### Citations

**File:** substrate/frame/revive/src/vm/evm/instructions/control.rs (L93-109)
```rust
fn return_inner<E: Ext>(
	interpreter: &mut Interpreter<E>,
	halt: impl Fn(Vec<u8>) -> Halt,
) -> ControlFlow<Halt> {
	let [offset, len] = interpreter.stack.popn()?;
	let len = as_usize_or_halt::<E::T>(len)?;

	// Important: Offset must be ignored if len is zeros
	let mut output = Default::default();
	if len != 0 {
		let offset = as_usize_or_halt::<E::T>(offset)?;
		interpreter.memory.resize(offset, len)?;
		output = interpreter.memory.slice_len(offset, len).to_vec()
	}

	ControlFlow::Break(halt(output))
}
```

**File:** substrate/frame/revive/src/vm/evm/instructions/system.rs (L206-222)
```rust
/// Common logic for copying data from a source buffer to the EVM's memory.
///
/// Handles memory expansion and gas calculation for data copy operations.
pub fn memory_resize<'a, E: Ext>(
	interpreter: &mut Interpreter<'a, E>,
	memory_offset: U256,
	len: usize,
) -> ControlFlow<Halt, Option<usize>> {
	if len == 0 {
		return ControlFlow::Continue(None);
	}

	interpreter.ext.charge_or_halt(RuntimeCosts::CopyToContract(len as u32))?;
	let memory_offset = as_usize_or_halt::<E::T>(memory_offset)?;
	interpreter.memory.resize(memory_offset, len)?;
	ControlFlow::Continue(Some(memory_offset))
}
```

**File:** substrate/frame/revive/src/exec.rs (L1412-1428)
```rust
			let mut output = match executable {
				ExecutableOrPrecompile::Executable(executable) => {
					executable.execute(self, entry_point, input_data)
				},
				ExecutableOrPrecompile::Precompile { instance, .. } => {
					instance.call(input_data, self)
				},
			}
			.and_then(|output| {
				if u32::try_from(output.data.len())
					.map(|len| len > limits::CALLDATA_BYTES)
					.unwrap_or(true)
				{
					Err(<Error<T>>::ReturnDataTooLarge)?;
				}
				Ok(output)
			})
```
