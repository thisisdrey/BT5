### Title
RETURNDATA corruption via overlapping `CALL`-into-memory writes reintroduces the 2021-08-27 minority-split bug - ([File: core/vm/instructions.go])

### Summary
`opCall`, `opCallCode`, `opDelegateCall`, and `opStaticCall` in `core/vm/instructions.go` write the callee's return data into the caller's memory via `scope.Memory.Set(retOffset, retSize, ret)` and then store the *same* `ret` slice as `evm.returnData`, without copying it first. When `ret` is a slice into the shared memory buffer (as is the case for the identity precompile at `0x04`, which returns its input slice unmodified — see `core/vm/contracts.go`), and the output write region overlaps but is shifted relative to the input region, the in-place `Memory.Set` mutation corrupts the backing array that `ret`/`evm.returnData` still points to. This is exactly the "RETURNDATA corruption via datacopy" bug that caused the real go-ethereum mainnet minority split on block 13107518 (2021-08-27), documented in this very repo at `docs/postmortems/2021-08-22-split-postmortem.md`.

### Finding Description
`core/vm/instructions.go` `opCall` (and its siblings `opCallCode`, `opDelegateCall`, `opStaticCall`): [1](#0-0) 

`args := scope.Memory.GetPtr(...)` returns a live pointer into memory (no copy) and the identity precompile returns that same slice as `ret` (unchanged design, confirmed by `core/vm/contracts.go`'s `dataCopy`). After the call returns, the code does:
```go
if err == nil || err == ErrExecutionReverted {
    scope.Memory.Set(retOffset.Uint64(), retSize.Uint64(), ret)
}
...
evm.returnData = ret
```
`Memory.Set` performs `copy(m.store[offset:offset+size], value)`: [2](#0-1) 

If `value` (`ret`) aliases the same backing array (`m.store`) as the destination range, and the two ranges overlap but are shifted, the byte-by-byte copy mutates the source bytes as it writes, so by the time the copy finishes, `ret`/`evm.returnData` no longer reflects the precompile's actual output — it reflects a partially self-overwritten buffer. Any subsequent `RETURNDATACOPY` (or any downstream logic depending on `evm.returnData`) observes corrupted data.

The upstream fix for this exact issue (applied in v1.10.8 following the 2021-08-27 incident) inserted `ret = common.CopyBytes(ret)` immediately before each `scope.Memory.Set(...)` call in all four CALL-variant opcodes, exactly as shown in the repo's own postmortem diff: [3](#0-2) 

That `common.CopyBytes(ret)` line is absent from the current `opCall`/`opCallCode`/`opDelegateCall`/`opStaticCall` implementations in this repo, meaning the historical fix has not been applied (or has been reverted), and the vulnerable aliasing pattern is reachable again.

### Impact Explanation
This breaks the equality between "what the EVM specification defines as RETURNDATA after a call" and "what geth actually stores." A contract can craft input such that `RETURNDATACOPY` (or any opcode relying on `evm.returnData`) reads corrupted bytes instead of the precompile's true output. Since this depends only on memory layout chosen by the calling contract (fully deterministic, no external input needed beyond the transaction itself), a single crafted transaction causes this node to compute a different result — and thus a different `stateRoot` — than a patched client executing the same transaction. This is a Critical severity issue: it is precisely the bug class that caused the real mainnet chain split in 2021, splitting nodes that had the bug from nodes that did not.

### Likelihood Explanation
Trivial to trigger deterministically: any account can send a transaction whose contract code performs a `CALL`/`STATICCALL`/etc. to the identity precompile (`0x04`) with an in-memory input/output window that overlaps but is shifted, then reads `RETURNDATACOPY` and stores the (corrupted) result to storage, causing a state divergence from any client that has the `common.CopyBytes` fix applied. No special privileges, no race conditions, and no reliance on other parties are required — this is a self-contained, always-reproducible discrepancy.

### Recommendation
Restore the upstream fix: in `core/vm/instructions.go`, before each `scope.Memory.Set(retOffset.Uint64(), retSize.Uint64(), ret)` call in `opCall`, `opCallCode`, `opDelegateCall`, and `opStaticCall`, copy `ret` into a fresh buffer (`ret = common.CopyBytes(ret)`) so that the memory write cannot mutate the slice that becomes `evm.returnData`.

### Proof of Concept
Based on the exploit pattern from the actual 2021 incident (`docs/postmortems/2021-08-22-split-postmortem.md`):
1. Deploy a contract that calls the identity precompile (address `0x04`) via `STATICCALL`/`CALL` with input memory `mem[0:4]` and output memory `mem[1:5]` (overlapping, shifted by 1 byte).
2. The precompile returns `mem[0:4]` unmodified as `ret` (aliasing the memory buffer).
3. `scope.Memory.Set(1, 4, ret)` copies `ret` into `mem[1:5]`, which — because `ret` is backed by the same array — self-corrupts as the copy proceeds.
4. Execute `RETURNDATACOPY` to load the (now-corrupted) `evm.returnData` into storage, and observe that the persisted storage value differs from what a client with the `CopyBytes` fix would compute for the identical transaction — producing a different `stateRoot`. [4](#0-3)

### Citations

**File:** core/vm/instructions.go (L757-768)
```go
	ret, result, err := evm.Call(scope.Contract.Address(), toAddr, args, childBudget, &value)

	if err != nil {
		temp.Clear()
	} else {
		temp.SetOne()
	}
	stack.push(&temp)

	if err == nil || err == ErrExecutionReverted {
		scope.Memory.Set(retOffset.Uint64(), retSize.Uint64(), ret)
	}
```

**File:** core/vm/memory.go (L54-66)
```go
// Set sets offset + size to value
func (m *Memory) Set(offset, size uint64, value []byte) {
	// It's possible the offset is greater than 0 and size equals 0. This is because
	// the calcMemSize (common.go) could potentially return 0 when size is zero (NO-OP)
	if size > 0 {
		// length of store may never be less than offset + size.
		// The store should be resized PRIOR to setting the memory
		if offset+size > uint64(len(m.store)) {
			panic("invalid memory: store empty")
		}
		copy(m.store[offset:offset+size], value)
	}
}
```

**File:** docs/postmortems/2021-08-22-split-postmortem.md (L30-45)
```markdown
```
1. Calling datacopy

  memory: [0, 1, 2, 3, 4]
  in (mem[0:4]) : [0,1,2,3]
  out (mem[1:5]): [1,2,3,4]

2. dataCopy returns

  returndata (==in, mem[0:4]): [0,1,2,3]
 
3. Copy in -> out

  => memory: [0,0,1,2,3]
  => returndata: [0,0,1,2]
```
```

**File:** docs/postmortems/2021-08-22-split-postmortem.md (L155-191)
```markdown
```diff
diff --git a/core/vm/instructions.go b/core/vm/instructions.go
index f7ef2f900e..6c8c6e6e6f 100644
--- a/core/vm/instructions.go
+++ b/core/vm/instructions.go
@@ -669,6 +669,7 @@ func opCall(pc *uint64, interpreter *EVMInterpreter, scope *ScopeContext) ([]byt
        }
        stack.push(&temp)
        if err == nil || err == ErrExecutionReverted {
+               ret = common.CopyBytes(ret)
                scope.Memory.Set(retOffset.Uint64(), retSize.Uint64(), ret)
        }
        scope.Contract.Gas += returnGas
@@ -703,6 +704,7 @@ func opCallCode(pc *uint64, interpreter *EVMInterpreter, scope *ScopeContext) ([
        }
        stack.push(&temp)
        if err == nil || err == ErrExecutionReverted {
+               ret = common.CopyBytes(ret)
                scope.Memory.Set(retOffset.Uint64(), retSize.Uint64(), ret)
        }
        scope.Contract.Gas += returnGas
@@ -730,6 +732,7 @@ func opDelegateCall(pc *uint64, interpreter *EVMInterpreter, scope *ScopeContext
        }
        stack.push(&temp)
        if err == nil || err == ErrExecutionReverted {
+               ret = common.CopyBytes(ret)
                scope.Memory.Set(retOffset.Uint64(), retSize.Uint64(), ret)
        }
        scope.Contract.Gas += returnGas
@@ -757,6 +760,7 @@ func opStaticCall(pc *uint64, interpreter *EVMInterpreter, scope *ScopeContext)
        }
        stack.push(&temp)
        if err == nil || err == ErrExecutionReverted {
+               ret = common.CopyBytes(ret)
                scope.Memory.Set(retOffset.Uint64(), retSize.Uint64(), ret)
        }
        scope.Contract.Gas += returnGas
```
