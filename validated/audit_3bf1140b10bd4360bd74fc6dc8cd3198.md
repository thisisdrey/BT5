### Title
EVM interpreter fetches/dereferences bytecode via raw pointers guarded only by `debug_assert!`, allowing an out-of-bounds instruction fetch analogous to the zkASM `dst == bytecodeLength` jump bug - (File: `substrate/frame/revive/src/vm/evm/ext_bytecode.rs`)

### Summary
`pallet-revive`'s EVM-compatibility interpreter (`substrate/frame/revive/src/vm/evm.rs`) tracks the program counter with a raw pointer (`instruction_pointer`) inside `ExtBytecode`. The functions that move this pointer and dereference it (`relative_jump`, `absolute_jump`, `read_slice`, `opcode`) rely on the *caller* to have validated that the resulting position is within bounds, but the only bounds enforcement present in the code is a `debug_assert!`, which is compiled out entirely in release/production builds. Worse, the one check that does exist uses `<=` rather than `<` when validating the new program counter, exactly mirroring the reported zkASM defect where `dst == bytecodeLength` is treated as "in bounds" right before the value is used to index/fetch a byte.

### Finding Description
The main interpreter loop unconditionally advances and re-reads the instruction stream every iteration: [1](#0-0) 

`opcode()` performs a raw dereference of `instruction_pointer` with **no bounds check of any kind**, not even a `debug_assert!`: [2](#0-1) 

The pointer is advanced by `relative_jump`, whose only guard is a `debug_assert!` that accepts `new_pc <= bytes.len()` — i.e. it explicitly allows the pointer to land exactly one byte past the end of the buffer, the identical boundary condition flagged in the external report (`dst == bytecodeLength`): [3](#0-2) 

`absolute_jump` (used by `JUMP`/`JUMPI`) and `read_slice` (used by `PUSH*`) follow the same pattern — `debug_assert!` only, using `<=`/`checked_add(...).map_or(false, |end| end <= bytes.len())`, then an unconditional `unsafe` pointer read/slice construction: [4](#0-3) 

Because on-chain runtimes are compiled in release mode, `debug_assert!` is a no-op there — the "check" the comments describe (`// SAFETY: The offset is validated by the caller...`) does not exist at runtime for the deployed chain at all. This is precisely the shape of the reported bug: a boundary check is present in the source/spec but the actual fetch/read that follows it does not enforce `dst < length` before consuming the value — here the situation is worse, since the enforcement mechanism itself (`debug_assert!`) is stripped from the artifact that actually executes on-chain, so the "check" never runs in production regardless of the comparison operator used.

### Impact Explanation
If the assumed invariant (`pc < bytecode.len()` before dereference) is ever violated by any caller — e.g., EVM bytecode that does not end in a terminating opcode (`STOP`/`RETURN`/`REVERT`/`INVALID`), or any future/undertested opcode path that calls `read_slice`/`absolute_jump` with an off-by-one length — `opcode()` and `read_slice()` will read past the allocated `Bytes` buffer via raw pointer arithmetic. This is undefined behavior in Rust (out-of-bounds read through `unsafe`), which in the worst case can produce memory disclosure or a non-deterministic execution result that differs across validator/collator implementations or backends (interpreter vs. compiler), an integrity break against "runtime bugs that compromise intended behavior" for a live Substrate-based chain, and a potential panic/crash of a public-facing contract-execution entrypoint (bare_call/eth_call/contract dispatch), i.e. degraded block production.

### Likelihood Explanation
Exploitability depends on whether the underlying `revm::bytecode::Bytecode` buffer is always padded with extra trailing zero bytes by the (external, out-of-repo) `revm` crate's analysis step — if it always is, the specific `pc == len` read is masked by that external padding and this local absence of a runtime check is latent rather than immediately triggerable. This repository's own code, however, provides **no** local, runtime-enforced guarantee for any of `relative_jump`, `absolute_jump`, or `read_slice` — the safety documentation asserts an invariant that only a compiled-out `debug_assert!` attempts to verify, and one of those checks (`relative_jump`) explicitly uses the same off-by-one-permissive `<=` comparison called out in the original report. Any change to bytecode padding assumptions in the `revm` dependency, or any interpreter code path that calls these functions with attacker-influenced offsets not already covered by `revm`'s own jump-table analysis, turns this into a directly reachable out-of-bounds read from unprivileged, unmodified public contract-execution entrypoints.

### Recommendation
Replace the `debug_assert!`-only guards in `ext_bytecode.rs` (`relative_jump`, `absolute_jump`, `read_slice`) with real runtime checks (`assert!`, or better, `Result`-returning fallible variants that translate to `Error::<T>::...` and halt execution), using strict `<` (not `<=`) against `bytecode.len()` for any position from which a byte will actually be read, matching the recommendation from the external report (`dst < bytecodeLength`, not `bytecodeLength < dst`). Do not rely on comments describing caller-side invariants as the sole safety mechanism for `unsafe` pointer arithmetic in code that runs on-chain in release mode.

### Proof of Concept
Conceptual PoC (requires confirming with a Devin session whether `revm`'s bytecode analysis pads the buffer, which determines actual triggerability):
1. Deploy EVM-compatible contract code via `pallet-revive`'s `from_evm_runtime_code` whose last opcode is not `STOP`/`RETURN`/`REVERT`/`INVALID` (e.g., raw code ending in `PUSH1 0x01` with no terminator, submitted as raw bytes bypassing normal compiler output, since `pallet-revive` accepts raw EVM bytecode uploads).
2. Call the contract so the interpreter's `run_plain` loop in `substrate/frame/revive/src/vm/evm.rs:161-167` executes every opcode up to the last byte, then calls `relative_jump(1)`, moving `instruction_pointer` to exactly `bytecode.len()`.
3. The loop immediately calls `interpreter.bytecode.opcode()` again (`ext_bytecode.rs:92-96`), performing `unsafe { *self.instruction_pointer }` at an address one byte past the validated allocation, with zero runtime bounds enforcement in the release build.
4. Observe non-deterministic opcode dispatch / potential panic depending on the state of the byte immediately following the `Bytes` allocation, demonstrating the missing production-time boundary check identical in structure to the reported `opJUMP`/`opJUMPI` bug.

### Citations

**File:** substrate/frame/revive/src/vm/evm.rs (L161-167)
```rust
fn run_plain<E: Ext>(interpreter: &mut Interpreter<E>) -> ControlFlow<Halt, Infallible> {
	loop {
		let opcode = interpreter.bytecode.opcode();
		interpreter.bytecode.relative_jump(1);
		exec_instruction(interpreter, opcode)?;
	}
}
```

**File:** substrate/frame/revive/src/vm/evm/ext_bytecode.rs (L54-66)
```rust
	/// Relative jumps does not require checking for overflow.
	pub fn relative_jump(&mut self, offset: isize) {
		// SAFETY: The offset is validated by the caller to ensure it points within the bytecode
		debug_assert!(
			{
				let bytes = self.base.bytes_ref();
				let new_pc = self.pc().wrapping_add_signed(offset);
				new_pc <= bytes.len()
			},
			"relative_jump would move instruction pointer out of bounds"
		);
		self.instruction_pointer = unsafe { self.instruction_pointer.offset(offset) };
	}
```

**File:** substrate/frame/revive/src/vm/evm/ext_bytecode.rs (L68-113)
```rust
	/// Absolute jumps require checking for overflow and if target is a jump destination
	/// from jump table.
	pub fn absolute_jump(&mut self, offset: usize) {
		// SAFETY: The offset is validated by the caller to ensure it points within the bytecode
		debug_assert!(
			offset <= self.base.bytes_ref().len(),
			"absolute_jump would move instruction pointer out of bounds"
		);
		self.instruction_pointer = unsafe { self.base.bytes_ref().as_ptr().add(offset) };
	}

	/// Check legacy jump destination from jump table.
	pub fn is_valid_legacy_jump(&mut self, offset: usize) -> bool {
		self.base.legacy_jump_table().expect("Panic if not legacy").is_valid(offset)
	}

	/// Returns current program counter.
	pub fn pc(&self) -> usize {
		// SAFETY: `instruction_pointer` should be at an offset from the start of the bytes.
		// In practice this is always true unless a caller modifies the `instruction_pointer` field
		// manually.
		unsafe { self.instruction_pointer.offset_from_unsigned(self.base.bytes_ref().as_ptr()) }
	}

	/// Returns instruction opcode.
	pub fn opcode(&self) -> u8 {
		// SAFETY: `instruction_pointer` always point to bytecode.
		unsafe { *self.instruction_pointer }
	}

	/// Reads next `len` bytes from the bytecode.
	///
	/// Used by PUSH opcode.
	pub fn read_slice(&self, len: usize) -> &[u8] {
		// SAFETY: The caller ensures that `len` bytes are available from the current instruction
		// pointer position.
		debug_assert!(
			{
				let bytes = self.base.bytes_ref();
				let pc = self.pc();
				pc.checked_add(len).map_or(false, |end| end <= bytes.len())
			},
			"read_slice would read out of bounds"
		);
		unsafe { core::slice::from_raw_parts(self.instruction_pointer, len) }
	}
```
