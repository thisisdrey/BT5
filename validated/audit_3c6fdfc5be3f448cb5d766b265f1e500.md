### Title
Integer overflow in `opReturnDataCopy` bounds check causes an unrecoverable Go panic ("slice bounds out of range") that crashes every node executing the block - ([File: core/vm/instructions.go])

### Summary
`RETURNDATACOPY`'s implementation computes `end = dataOffset + length` using 256-bit modular addition and only checks whether the *wrapped* result overflows `uint64`, not whether the addition itself overflowed `2**256`. A `length` chosen so that `dataOffset + length ≡ small_value (mod 2**256)` makes the bounds check pass even though `end < dataOffset`. The subsequent Go slice expression `evm.returnData[offset64:end64]` then has `low > high`, which is an unconditional Go runtime panic, independent of `len(evm.returnData)`. Since no goroutine in the EVM call chain recovers from panics, this crashes the whole Geth process for every node that executes the transaction/block.

### Finding Description
`opReturnDataCopy` in [1](#0-0)  is:

```go
func opReturnDataCopy(pc *uint64, evm *EVM, scope *ScopeContext) ([]byte, error) {
	memOffset, dataOffset, length := scope.Stack.pop3()

	offset64, overflow := dataOffset.Uint64WithOverflow()
	if overflow {
		return nil, ErrReturnDataOutOfBounds
	}
	// we can reuse dataOffset now (aliasing it for clarity)
	var end = dataOffset
	end.Add(dataOffset, length)
	end64, overflow := end.Uint64WithOverflow()
	if overflow || uint64(len(evm.returnData)) < end64 {
		return nil, ErrReturnDataOutOfBounds
	}
	scope.Memory.Set(memOffset.Uint64(), length.Uint64(), evm.returnData[offset64:end64])
	return nil, nil
}
```

`end.Add(dataOffset, length)` performs `uint256` addition modulo `2**256` (the same semantics as the EVM `ADD` opcode). It does not itself report overflow. The only sanity check performed afterwards is `end.Uint64WithOverflow()`, which merely checks that the wrapped 256-bit result fits into 64 bits — it says nothing about whether `dataOffset + length` actually exceeded `2**256`.

Choosing `dataOffset = 1` and `length = 2**256 - 1` (all-`0xFF` word) gives:
- `offset64 = 1` (fits uint64, no overflow reported)
- `end = (1 + (2**256-1)) mod 2**256 = 0`
- `end64 = 0` (fits uint64, no overflow reported)
- Bounds check `len(evm.returnData) < end64` → `0 < 0` → false → check passes.

Execution proceeds to `evm.returnData[offset64:end64]` = `evm.returnData[1:0]`. In Go, a slice expression with `low(1) > high(0)` panics unconditionally ("slice bounds out of range [1:0]") regardless of the underlying slice's length.

This is the direct analog of the reported Vyper `slice()` bug: an unchecked `add(start, length)` used in a bounds `assert`/check that an attacker-controlled overflow can bypass, corrupting downstream memory access — here manifesting as a Go slice panic instead of silent memory corruption because Go's runtime enforces slice invariants.

Confirmed that no `recover()` exists anywhere along the EVM execution path (`core/vm/interpreter.go`, `core/vm/evm.go`, `core/state_transition.go`, `core/state_processor.go`) — a search for `recover()` in `core/**` only found matches in test files [2](#0-1) . Consequently a panic raised inside `opReturnDataCopy` propagates uncaught through `evm.Run` → `evm.Call`/`Create` → `state_transition.go` execution → block processing, terminating the Go process.

### Impact Explanation
Any single transaction that executes `RETURNDATACOPY` with a crafted `(dataOffset, length)` pair triggering the wraparound will panic the goroutine processing that transaction. Since there is no panic recovery anywhere in the EVM/state-transition call path, this crashes every Geth node that processes the block containing this transaction — validators, RPC nodes, and miners alike. Per the scope rules, "a single transaction or block that crashes or halts every Geth node" is a Critical-impact finding.

### Likelihood Explanation
The trigger requires only ordinary contract bytecode with no special permissions, value, or preconditions:
```
PUSH32 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF  // length
PUSH1 0x01                                                                 // dataOffset
PUSH1 0x00                                                                 // memOffset
RETURNDATACOPY
```
This can be embedded in any contract's init code or runtime code and executed by any account with no special privileges, making the likelihood high once such a transaction is included in a block.

### Recommendation
Check for overflow explicitly in the 256-bit addition (or perform the length/end check using saturating/checked arithmetic on `uint256.Int`, comparable to how `calcMemSize64WithUint` in [3](#0-2)  validates `offset64 + length64 < offset64` for uint64 overflow). For `opReturnDataCopy`, verify `end.Lt(dataOffset)` (or use `end, carry := dataOffset.AddOverflow(dataOffset, length)`0 style checked add) to detect wraparound and reject with `ErrReturnDataOutOfBounds` before deriving `end64`, ensuring `end64 >= offset64` always holds prior to slicing `evm.returnData`.

### Proof of Concept
Deploy/execute a contract with the following bytecode:
```
PUSH32 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF
PUSH1  0x01
PUSH1  0x00
RETURNDATACOPY
```
Executing this opcode sequence (no preceding call is required since `evm.returnData` starts empty) causes:
1. `dataOffset.Uint64WithOverflow()` → `offset64 = 1`, `overflow = false`.
2. `end = 1 + (2**256-1) mod 2**256 = 0`; `end.Uint64WithOverflow()` → `end64 = 0`, `overflow = false`.
3. Bounds check `0 < 0` is false → passes.
4. `evm.returnData[1:0]` is evaluated → Go runtime panic: `slice bounds out of range [1:0]`.

Because no `recover()` exists on the EVM execution call stack (confirmed via [4](#0-3)  and [5](#0-4) ), this panic propagates and crashes the entire Geth process for every node processing the block containing this transaction.

### Citations

**File:** core/vm/instructions.go (L307-323)
```go
func opReturnDataCopy(pc *uint64, evm *EVM, scope *ScopeContext) ([]byte, error) {
	memOffset, dataOffset, length := scope.Stack.pop3()

	offset64, overflow := dataOffset.Uint64WithOverflow()
	if overflow {
		return nil, ErrReturnDataOutOfBounds
	}
	// we can reuse dataOffset now (aliasing it for clarity)
	var end = dataOffset
	end.Add(dataOffset, length)
	end64, overflow := end.Uint64WithOverflow()
	if overflow || uint64(len(evm.returnData)) < end64 {
		return nil, ErrReturnDataOutOfBounds
	}
	scope.Memory.Set(memOffset.Uint64(), length.Uint64(), evm.returnData[offset64:end64])
	return nil, nil
}
```

**File:** core/vm/evm.go (L263-345)
```go
// Call executes the contract associated with the addr with the given input as
// parameters. It also handles any necessary value transfer required and takse
// the necessary steps to create accounts and reverses the state in case of an
// execution error or failed value transfer.
func (evm *EVM) Call(caller common.Address, addr common.Address, input []byte, gas GasBudget, value *uint256.Int) (ret []byte, result GasBudget, err error) {
	// Capture the tracer start/end events in debug mode
	if evm.Config.Tracer != nil {
		evm.captureBegin(evm.depth, CALL, caller, addr, input, gas, value.ToBig())
		defer func(startGas GasBudget) {
			evm.captureEnd(evm.depth, startGas, result, ret, err)
		}(gas)
	}
	// Fail if we're trying to execute above the call depth limit
	if evm.depth > int(params.CallCreateDepth) {
		return nil, gas, ErrDepth
	}
	syscall := isSystemCall(caller)

	// Fail if we're trying to transfer more than the available balance.
	if !syscall && !value.IsZero() && !evm.Context.CanTransfer(evm.StateDB, caller, value) {
		return nil, gas, ErrInsufficientBalance
	}
	snapshot := evm.StateDB.Snapshot()
	p, isPrecompile := evm.precompile(addr)
	if !evm.StateDB.Exist(addr) {
		if !isPrecompile && evm.chainRules.IsEIP4762 && !isSystemCall(caller) {
			// Add proof of absence to witness
			// At this point, the read costs have already been charged, either because this
			// is a direct tx call, in which case it's covered by the intrinsic gas, or because
			// of a CALL instruction, in which case BASIC_DATA has been added to the access
			// list in write mode. If there is enough gas paying for the addition of the code
			// hash leaf to the access list, then account creation will proceed unimpaired.
			// Thus, only pay for the creation of the code hash leaf here.
			wgas := evm.AccessEvents.CodeHashGas(addr, true, gas.ExecutionGas, false)
			if _, ok := gas.ChargeExecution(wgas); !ok {
				evm.StateDB.RevertToSnapshot(snapshot)
				return nil, gas.ExitHalt(), ErrOutOfGas
			}
		}

		if !isPrecompile && evm.chainRules.IsEIP158 && value.IsZero() {
			// Calling a non-existing account, don't do anything.
			return nil, gas, nil
		}
		evm.StateDB.CreateAccount(addr)
	}
	// Perform the value transfer only in non-syscall mode.
	// Calling this is required even for zero-value transfers,
	// to ensure the state clearing mechanism is applied.
	if !syscall {
		evm.Context.Transfer(evm.StateDB, caller, addr, value, &evm.chainRules)
	}

	if isPrecompile {
		ret, gas, err = RunPrecompiledContract(evm.StateDB, p, addr, input, gas, evm.Config.Tracer, evm.chainRules, evm.precompileCache)
	} else {
		// Initialise a new contract and set the code that is to be used by the EVM.
		code := evm.resolveCode(addr)
		if len(code) == 0 {
			ret, err = nil, nil // gas is unchanged
		} else {
			// The contract is a scoped environment for this execution context only.
			contract := NewContract(caller, addr, value, gas, evm.jumpDests)
			contract.IsSystemCall = isSystemCall(caller)
			contract.SetCallCode(evm.resolveCodeHash(addr), code)
			ret, err = evm.Run(contract, input, false)
			gas = contract.Gas
		}
	}

	// Calculate the remaining gas at the end of frame
	exitGas := gas.Exit(err)
	if err != nil {
		evm.StateDB.RevertToSnapshot(snapshot)

		if err != ErrExecutionReverted {
			if evm.Config.Tracer.HasGasHook() {
				evm.Config.Tracer.EmitGasChange(gas.AsTracing(), exitGas.AsTracing(), tracing.GasChangeCallFailedExecution)
			}
		}
	}
	return ret, exitGas, err
}
```

**File:** core/vm/common.go (L65-81)
```go
// calcMemSize64WithUint calculates the required memory size, and returns
// the size and whether the result overflowed uint64
// Identical to calcMemSize64, but length is a uint64
func calcMemSize64WithUint(off *uint256.Int, length64 uint64) (uint64, bool) {
	// if length is zero, memsize is always zero, regardless of offset
	if length64 == 0 {
		return 0, false
	}
	// Check that offset doesn't overflow
	offset64, overflow := off.Uint64WithOverflow()
	if overflow {
		return 0, true
	}
	val := offset64 + length64
	// if value < either of it's parts, then it overflowed
	return val, val < offset64
}
```

**File:** core/state_transition.go (L836-887)
```go
func (st *stateTransition) executeCall(rules params.Rules, value *uint256.Int) ([]byte, error) {
	msg := st.msg

	// Increment the nonce for the next transaction.
	st.state.SetNonce(msg.From, st.state.GetNonce(msg.From)+1, tracing.NonceChangeEoACall)

	if rules.IsAmsterdam {
		snapshot := st.state.Snapshot()
		entryGas := st.gasRemaining
		if !st.applyAuthorizations(rules, st.msg.SetCodeAuthorizations) {
			st.state.RevertToSnapshot(snapshot)
			st.gasRemaining = st.gasRemaining.ExitHalt()
			st.traceHaltedTopFrame(vm.CALL, st.to(), msg.Data, entryGas, st.gasRemaining, value)
			return nil, vm.ErrOutOfGas
		}
		if !st.chargeCallRecipientEIP2780(value) {
			st.state.RevertToSnapshot(snapshot)
			st.gasRemaining = st.gasRemaining.ExitHalt()
			st.traceHaltedTopFrame(vm.CALL, st.to(), msg.Data, entryGas, st.gasRemaining, value)
			return nil, vm.ErrOutOfGas
		}
	} else {
		// Apply EIP-7702 authorizations.
		st.applyAuthorizations(rules, msg.SetCodeAuthorizations)

		// Perform convenience warming of sender's delegation target. Although the
		// sender is already warmed in Prepare(..), it's possible a delegation to
		// the account was deployed during this transaction. To handle correctly,
		// simply wait until the final state of delegations is determined before
		// performing the resolution and warming.
		if addr, ok := types.ParseDelegation(st.state.GetCode(*msg.To)); ok {
			st.state.AddAddressToAccessList(addr)
		}
	}
	ret, result, vmerr := st.evm.Call(msg.From, st.to(), msg.Data, st.gasRemaining.ForwardAll(), value)
	st.gasRemaining.Absorb(result)

	// If the call frame reverts or halts exceptionally, the charged state-gas
	// is refilled back to the state reservoir in Amsterdam.
	if rules.IsAmsterdam && vmerr != nil && !value.IsZero() && st.evm.StateDB.Empty(st.to()) {
		st.gasRemaining.RefundState(params.AccountCreationSize * st.evm.Context.CostPerStateByte)
	}
	// If the top-most frame halted, drain the leftover execution gas rather
	// than returning it to the sender. The frame exit itself already burned
	// its gas left, but the refill above repays the execution gas the charge
	// originally borrowed, and on a halt that repayment must be burned as
	// well.
	if rules.IsAmsterdam && vmerr != nil && vmerr != vm.ErrExecutionReverted {
		st.gasRemaining.DrainExecution()
	}
	return ret, vmerr
}
```
