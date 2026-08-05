Audit Report

## Title
CREATE/CREATE2 initcode-size check uses hardcoded EIP-3860 mainnet constant instead of the chain's actual configured code-size limit - (File: `substrate/frame/revive/src/vm/evm/instructions/contract.rs`)

## Summary
The EVM `CREATE`/`CREATE2` opcode implementation in `create()` rejects initcode above the hardcoded Ethereum-mainnet constant `revm::primitives::eip3860::MAX_INITCODE_SIZE` (49152 bytes), and the equivalent runtime-code path in `ContractBlob::from_evm_runtime_code_with_deposit` uses `revm::primitives::eip170::MAX_CODE_SIZE`, rather than deriving these limits from the pallet's own configured ceiling `limits::code::BLOB_BYTES` (1 MiB). This causes EVM contract creation to fail for initcode/runtime code between ~24-49KB and 1MiB even though the chain is provisioned to store and execute blobs up to `BLOB_BYTES`.

## Finding Description
In `create()`, the length check compares directly against the imported mainnet constant: [1](#0-0) 
The same hardcoded pattern appears in `ContractBlob::from_evm_init_code` (using `eip3860::MAX_INITCODE_SIZE`) and `from_evm_runtime_code_with_deposit` (using `eip170::MAX_CODE_SIZE`): [2](#0-1) [3](#0-2) 

Meanwhile the pallet's own configured maximum code blob size is `limits::code::BLOB_BYTES = 1024 * 1024`, enforced via `limits::code::enforce()`: [4](#0-3) 

I confirmed via `grep_search` that `limits::code::enforce` is called only from `substrate/frame/revive/src/vm/pvm/env.rs` — i.e., it is exclusively part of the native PVM (PolkaVM) code-upload path, not the EVM code path. The EVM `CREATE`/`CREATE2` and code-upload functions (`from_evm_init_code`, `from_evm_runtime_code_with_deposit`) never call `limits::code::enforce` or reference `BLOB_BYTES` at all; they only check against the hardcoded `revm::primitives::eip3860`/`eip170` mainnet constants. The only bypass is the `DebugSettings::is_unlimited_contract_size_allowed` flag, which is a governance/debug-mode gate, not a fix to the underlying mismatch.

## Impact Explanation
This is a real code-path discrepancy: EVM `CREATE`/`CREATE2` calls with initcode/runtime code between the EIP-3860/170 mainnet constants (~24576/49152 bytes) and the chain's actual `BLOB_BYTES` capacity (1 MiB) will unconditionally revert with `BlobTooLarge`, even though such code would fit within the pallet's real configured storage/execution ceiling. Because CREATE2 addresses are deterministic, pre-funding a never-deployable CREATE2 address is a plausible pattern, and permanent fund lock at that address is a genuine, if narrow, consequence. However, this does not compromise settlement correctness, origin/authority binding, proof verification, or fund custody of already-deployed contracts — it is a functionality/availability limitation (a stricter-than-necessary size gate) rather than a broken invariant that leads to theft, duplicate payout, or unauthorized execution. It matches the "permanent user-fund lock" impact class only in the narrow pre-funded-CREATE2-address scenario, which requires a specific and unusual deployment pattern (pre-sending funds to a not-yet-deployed address with oversized initcode) rather than being a generally reachable exploit against arbitrary users' funds.

## Likelihood Explanation
The size mismatch itself is deterministically reachable by any unprivileged EVM caller submitting `CREATE`/`CREATE2` with initcode in the affected size range — no special privileges are required to hit the `BlobTooLarge` revert. However, actual fund loss requires the additional precondition of a counterparty pre-funding a computed CREATE2 address before deployment with oversized initcode, which is a specific integration pattern, not a universal condition. The core check behavior (rejecting legitimate deployments that would fit under `BLOB_BYTES`) is fully reproducible and always reachable.

## Recommendation
Replace the hardcoded `revm::primitives::eip3860::MAX_INITCODE_SIZE` and `revm::primitives::eip170::MAX_CODE_SIZE` comparisons in `create()` (`contract.rs`) and in `ContractBlob::from_evm_init_code` / `from_evm_runtime_code_with_deposit` (`evm.rs`) with checks against `crate::limits::code::BLOB_BYTES` (or a dedicated Config-exposed limit), so EVM initcode/runtime-code size checks are bound to the chain's actual configured capacity.

## Proof of Concept
1. On a pallet-revive chain with default `BLOB_BYTES = 1 MiB`, craft EVM initcode with length `L` such that `49152 < L <= 1_048_576`.
2. Call `CREATE2` from an unprivileged EOA/contract with this initcode, with `DebugSettings::is_unlimited_contract_size_allowed` at its default (disabled) value.
3. Observe `create()` in `substrate/frame/revive/src/vm/evm/instructions/contract.rs` (lines 58-63) unconditionally returns `Error::BlobTooLarge`, despite `L` being well within `limits::code::BLOB_BYTES`.
4. (Optional, for fund-lock scenario) Precompute the CREATE2 address and transfer funds to it before deployment; deployment then permanently fails, leaving funds stranded at that address.

### Citations

**File:** substrate/frame/revive/src/vm/evm/instructions/contract.rs (L56-63)
```rust
	let mut code = Vec::new();
	if len != 0 {
		// EIP-3860: Limit initcode
		if len > revm::primitives::eip3860::MAX_INITCODE_SIZE &&
			!DebugSettings::is_unlimited_contract_size_allowed::<E::T>()
		{
			return ControlFlow::Break(Error::<E::T>::BlobTooLarge.into());
		}
```

**File:** substrate/frame/revive/src/vm/evm.rs (L66-71)
```rust
	pub fn from_evm_init_code(code: Vec<u8>, owner: AccountIdOf<T>) -> Result<Self, DispatchError> {
		if code.len() > revm::primitives::eip3860::MAX_INITCODE_SIZE &&
			!DebugSettings::is_unlimited_contract_size_allowed::<T>()
		{
			return Err(<Error<T>>::BlobTooLarge.into());
		}
```

**File:** substrate/frame/revive/src/vm/evm.rs (L121-125)
```rust
		if code.len() > revm::primitives::eip170::MAX_CODE_SIZE &&
			!DebugSettings::is_unlimited_contract_size_allowed::<T>()
		{
			return Err(<Error<T>>::BlobTooLarge.into());
		}
```

**File:** substrate/frame/revive/src/limits.rs (L109-149)
```rust
	/// The maximum length of a code blob in bytes.
	///
	/// This mostly exist to prevent parsing too big blobs and to
	/// have a maximum encoded length.
	pub const BLOB_BYTES: u32 = 1024 * 1024;

	/// The maximum amount of memory the interpreter is allowed to use for compilation artifacts.
	pub const INTERPRETER_CACHE_BYTES: u32 = 1024 * 1024;

	/// The maximum size of a basic block in number of instructions.
	///
	/// We need to limit the size of basic blocks because the interpreters lazy compilation
	/// compiles one basic block at a time. A malicious program could trigger the compilation
	/// of the whole program by creating one giant basic block otherwise.
	pub const BASIC_BLOCK_SIZE: u32 = 1000;

	/// The limit for memory that can be purged on demand.
	///
	/// We purge this memory every time we call into another contract.
	/// Hence we effectively only need to hold it once in RAM.
	pub const PURGABLE_MEMORY_LIMIT: u32 = INTERPRETER_CACHE_BYTES + 2 * 1024 * 1024;

	/// The limit for memory that needs to be kept alive for a contracts whole life time.
	///
	/// This means tuning this number affects the call stack depth.
	pub const BASELINE_MEMORY_LIMIT: u32 = BLOB_BYTES + 512 * 1024;

	/// Make sure that the various program parts are within the defined limits.
	pub fn enforce<T: Config>(
		pvm_blob: Vec<u8>,
		available_syscalls: &[&[u8]],
	) -> Result<Vec<u8>, DispatchError> {
		use polkavm_common::program::{
			EstimateInterpreterMemoryUsageArgs, ISA_ReviveV1, InstructionSetKind,
		};

		let len: u64 = pvm_blob.len() as u64;
		if len > crate::limits::code::BLOB_BYTES.into() {
			log::debug!(target: LOG_TARGET, "contract blob too large: {len} limit: {BLOB_BYTES}");
			return Err(<Error<T>>::BlobTooLarge.into());
		}
```
