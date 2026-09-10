### Title
Unchecked system-call error in `ProcessBeaconBlockRoot` allows a block with a failed EIP-4788 beacon-root write to be treated as valid - (File: `core/state_processor.go`)

### Summary
`ProcessBeaconBlockRoot`, which implements the EIP-4788 system call that stores the parent beacon block root in the `BeaconRootsAddress` contract, discards the error returned by `evm.Call` entirely [1](#0-0) . This is exactly the "silent failure" pattern described in the external report: a call that can fail (Compound's `mToken.mint` returning an error code instead of reverting) is invoked without checking its outcome, so a failure does not propagate and the caller proceeds as if everything succeeded.

### Finding Description
`ProcessBeaconBlockRoot` builds the system message and invokes:

```go
_, _, _ = evm.Call(msg.From, *msg.To, msg.Data, gasBudget, common.U2560)
``` [2](#0-1) 

The returned `err` is discarded, so regardless of whether the call to `BeaconRootsAddress` succeeds, reverts, or halts (out-of-gas, invalid opcode, depth error, etc.), execution continues: the state is `Finalise`d and merged into the block access list as if the beacon-root write had succeeded [3](#0-2) .

Compare this to the structurally identical sibling function `ProcessParentBlockHash`, which implements the analogous EIP-2935 system call and explicitly checks the error and panics if the call fails:

```go
_, _, err := evm.Call(msg.From, *msg.To, msg.Data, gasBudget, common.U2560)
if err != nil {
    panic(err)
}
``` [4](#0-3) 

The general-purpose system call helper used for the withdrawal/consolidation/deposit queues (`processRequestsSystemCall`) also checks the error and turns it into a hard failure (`fmt.Errorf("system call failed to execute: %v", err)`) that aborts block/receipt construction [5](#0-4) .

`ProcessBeaconBlockRoot` is the only one of the four system-call sites in this file that swallows the call's error with `_, _, _ =`. Per the equality this scan cares about, EIP-4788's system-call semantics require that a failing system call make the block invalid (mirrored by the `panic`/error-propagation behavior of the other three call sites in this same file). By ignoring the error, Geth can persist a `stateRoot`/head for a block whose beacon-root system call actually failed, while a spec-conformant implementation (or Geth's own sibling functions) would reject that block — breaking the "block a spec-conformant client accepts/rejects" and "persisted state matches what was executed" equalities.

### Impact Explanation
If the EIP-4788 system call ever fails (e.g., due to an unexpected code path returning `ErrOutOfGas`, `ErrDepth`, or a revert triggered by state that the beacon-roots ring-buffer contract cannot handle), Geth will still finalize state and produce a block header/stateRoot as if the beacon root had been correctly recorded, whereas the EIP mandates the block be treated as invalid on system-call failure. This is a High-severity persistence/consensus-equality mismatch class: Geth would build/accept a block that does not match what other spec-following clients (or the correct sibling implementations of the same file) would do, and a proposer using this code path could produce an invalid block without noticing.

### Likelihood Explanation
Likelihood in practice is low: the `BeaconRootsAddress` contract is fixed, minimal bytecode, called with a generous 30,000,000 gas budget and zero value, so under normal mainnet conditions the call essentially never fails. However, the code path is unconditionally silent regardless of the reason for failure, so any future change to gas accounting, EIP-4762 witnessing costs, or contract state that causes the call to revert/halt would go completely undetected, unlike the exactly analogous EIP-2935 call in the same file which is defensively checked.

### Recommendation
Check the error returned by `evm.Call` in `ProcessBeaconBlockRoot` and propagate/`panic` in the same way `ProcessParentBlockHash` does, so a failing EIP-4788 system call cannot be silently absorbed into a "successful" block.

### Proof of Concept
Not independently reproducible from static analysis alone — the only way to hit the discarded error is to make the `BeaconRootsAddress` call fail (e.g. via a code/gas-accounting change that causes `evm.Call` to return `ErrOutOfGas`/`ErrDepth`/an execution revert for the fixed system-call parameters). This report is a code-correctness finding based on directly comparing `ProcessBeaconBlockRoot` [6](#0-5)  against the otherwise identical, error-checked `ProcessParentBlockHash` [7](#0-6)  and `processRequestsSystemCall` [8](#0-7)  in the same file; I was unable to verify via git history whether this discrepancy is a pre-existing, deliberate upstream choice or was introduced in this fork, since the blame tool call failed and no further tool calls were available.

### Citations

**File:** core/state_processor.go (L313-341)
```go
// ProcessBeaconBlockRoot applies the EIP-4788 system call to the beacon block root
// contract. This method is exported to be used in tests.
func ProcessBeaconBlockRoot(beaconRoot common.Hash, evm *vm.EVM, blockAccessList *bal.ConstructionBlockAccessList) {
	if tracer := evm.Config.Tracer; tracer != nil {
		onSystemCallStart(tracer, evm.GetVMContext())
		if tracer.OnSystemCallEnd != nil {
			defer tracer.OnSystemCallEnd()
		}
	}
	gasLimit, gasBudget := systemCallGasBudget(evm)
	msg := &Message{
		From:      params.SystemAddress,
		GasLimit:  gasLimit,
		GasPrice:  uint256.NewInt(0),
		GasFeeCap: uint256.NewInt(0),
		GasTipCap: uint256.NewInt(0),
		To:        &params.BeaconRootsAddress,
		Data:      beaconRoot[:],
	}
	evm.SetTxContext(NewEVMTxContext(msg))
	evm.StateDB.Prepare(evm.GetRules(), common.Address{}, common.Address{}, nil, nil, nil)
	evm.StateDB.SetTxContext(common.Hash{}, 0, 0)
	evm.StateDB.AddAddressToAccessList(params.BeaconRootsAddress)
	_, _, _ = evm.Call(msg.From, *msg.To, msg.Data, gasBudget, common.U2560)
	if evm.StateDB.AccessEvents() != nil {
		evm.StateDB.AccessEvents().Merge(evm.AccessEvents)
	}
	blockAccessList.Merge(evm.StateDB.Finalise(evm.GetRules()))
}
```

**File:** core/state_processor.go (L343-374)
```go
// ProcessParentBlockHash stores the parent block hash in the history storage contract
// as per EIP-2935/7709.
func ProcessParentBlockHash(prevHash common.Hash, evm *vm.EVM, blockAccessList *bal.ConstructionBlockAccessList) {
	if tracer := evm.Config.Tracer; tracer != nil {
		onSystemCallStart(tracer, evm.GetVMContext())
		if tracer.OnSystemCallEnd != nil {
			defer tracer.OnSystemCallEnd()
		}
	}
	gasLimit, gasBudget := systemCallGasBudget(evm)
	msg := &Message{
		From:      params.SystemAddress,
		GasLimit:  gasLimit,
		GasPrice:  uint256.NewInt(0),
		GasFeeCap: uint256.NewInt(0),
		GasTipCap: uint256.NewInt(0),
		To:        &params.HistoryStorageAddress,
		Data:      prevHash.Bytes(),
	}
	evm.SetTxContext(NewEVMTxContext(msg))
	evm.StateDB.Prepare(evm.GetRules(), common.Address{}, common.Address{}, nil, nil, nil)
	evm.StateDB.SetTxContext(common.Hash{}, 0, 0)
	evm.StateDB.AddAddressToAccessList(params.HistoryStorageAddress)
	_, _, err := evm.Call(msg.From, *msg.To, msg.Data, gasBudget, common.U2560)
	if err != nil {
		panic(err)
	}
	if evm.StateDB.AccessEvents() != nil {
		evm.StateDB.AccessEvents().Merge(evm.AccessEvents)
	}
	blockAccessList.Merge(evm.StateDB.Finalise(evm.GetRules()))
}
```

**File:** core/state_processor.go (L400-431)
```go
func processRequestsSystemCall(requests *[][]byte, rules params.Rules, evm *vm.EVM, requestType byte, addr common.Address, blockAccessIndex uint32, blockAccessList *bal.ConstructionBlockAccessList) error {
	if evm.StateDB.GetCodeSize(addr) == 0 {
		return fmt.Errorf("empty system contract: no code at %v", addr)
	}
	if tracer := evm.Config.Tracer; tracer != nil {
		onSystemCallStart(tracer, evm.GetVMContext())
		if tracer.OnSystemCallEnd != nil {
			defer tracer.OnSystemCallEnd()
		}
	}
	gasLimit, gasBudget := systemCallGasBudget(evm)
	msg := &Message{
		From:      params.SystemAddress,
		GasLimit:  gasLimit,
		GasPrice:  uint256.NewInt(0),
		GasFeeCap: uint256.NewInt(0),
		GasTipCap: uint256.NewInt(0),
		To:        &addr,
	}
	evm.SetTxContext(NewEVMTxContext(msg))
	evm.StateDB.Prepare(evm.GetRules(), common.Address{}, common.Address{}, nil, nil, nil)
	evm.StateDB.SetTxContext(common.Hash{}, 0, blockAccessIndex)
	evm.StateDB.AddAddressToAccessList(addr)
	ret, _, err := evm.Call(msg.From, *msg.To, msg.Data, gasBudget, common.U2560)
	if evm.StateDB.AccessEvents() != nil {
		evm.StateDB.AccessEvents().Merge(evm.AccessEvents)
	}
	bal := evm.StateDB.Finalise(evm.GetRules())
	if err != nil {
		return fmt.Errorf("system call failed to execute: %v", err)
	}
	blockAccessList.Merge(bal)
```
