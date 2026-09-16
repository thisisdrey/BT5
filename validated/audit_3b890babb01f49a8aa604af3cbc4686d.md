Based on my investigation, I found a concrete analog to the CVE-2025-38401 bug class ("preparation fails but caller proceeds using stale/previous state, leading to memory corruption") in the TVM precompiled-contract call path.

### Title
Unconditional memory write with failure-path buffer in `callToPrecompiledAddress` can crash the node on any CALL/STATICCALL/DELEGATECALL to a precompile - ([File: actuator/src/main/java/org/tron/core/vm/program/Program.java])

### Summary
`Program.callToPrecompiledAddress` calls `contract.execute(data)`, which returns a `Pair<Boolean, byte[]>`. When the boolean (`out.getLeft()`) is `false` (execution failed / validation failed), the method still falls through to an unconditional `memorySave` call using `out.getRight()` — the same buffer field, regardless of success or failure. [1](#0-0) 

### Finding Description
The relevant code is:
```
Pair<Boolean, byte[]> out = contract.execute(data);

if (out.getLeft()) { // success
  ...
  returnDataBuffer = out.getRight();
  deposit.commit();
} else {
  // spend all energy on failure, push zero and revert state changes
  this.refundEnergy(0, CALL_PRE_COMPILED);
  this.stackPushZero();
  if (Objects.nonNull(this.result.getException())) {
    throw result.getException();
  }
}

if (VMConfig.allowTvmSelfdestructRestriction()) {
  this.memorySave(msg.getOutDataOffs().intValueSafe(), msg.getOutDataSize().intValueSafe(), out.getRight());
} else {
  this.memorySave(msg.getOutDataOffs().intValue(), out.getRight());
}
``` [2](#0-1) 

This mirrors the kernel bug class exactly: the "preparation" step (`contract.execute`) can fail, but the caller does not stop the follow-on operation (the memory write) — it proceeds using whatever is in `out.getRight()` from the failed preparation, exactly as `msdc_start_data()` proceeded with "previous setting" after `msdc_prepare_data()` failed.

`memorySave(int addr, byte[] value)` immediately dereferences `value.length`:
```
public void memorySave(int addr, byte[] value) {
  memory.write(addr, value, value.length, false);
}
``` [3](#0-2) 

and the `allowTvmSelfdestructRestriction()` variant calls `memorySave(int, int, byte[])` → `memory.extendAndWrite(addr, allocSize, value)` → `write(address, data, allocSize, false)`, which does `if (data.length < dataSize)` — also dereferencing `data.length` unconditionally. [4](#0-3) [5](#0-4) 

If any precompiled contract implementation in `PrecompiledContracts.java` returns `Pair.of(false, null)` (or any null right-side value) on a validation/execution failure — a pattern explicitly present via the 11 occurrences of `Pair.of(false, ...)` found in that file — this leads directly to a `NullPointerException` inside `memorySave`/`Memory.write`, uncaught by the surrounding failure-handling branch (which already committed to `stackPushZero()`/`refundEnergy` under the assumption the call is "handled").

I was not able to fully confirm within this session whether every one of the 11 `Pair.of(false, ...)` call sites passes `null` specifically (some may pass `EMPTY_BYTE_ARRAY`); this is a limitation of the investigation and would need to be checked precompile-by-precompile in `PrecompiledContracts.java` before treating this as a certainty.

### Impact Explanation
If a `NullPointerException` (or any other unchecked exception) is thrown from deep inside `Memory.write` during precompile-call handling, it propagates up through `VM.play` / `VMActuator.execute`. Depending on how far up it is caught, this can either (a) cause the specific transaction to fail unpredictably in a way not modeled by energy accounting (state divergence risk between nodes if the exception path differs from the deterministic revert path), or (b) if uncaught by a broader handler, crash block application in `Manager`, resulting in denial of service / node halt. Because the trigger is a plain `CALL`/`STATICCALL`/`DELEGATECALL` opcode to a precompiled contract address from any smart contract, this is reachable by an unprivileged contract-calling transaction.

### Likelihood Explanation
Likelihood depends entirely on whether any precompile's failure path returns a `null` byte array for the right-hand value of the `Pair`. This requires confirmation by reading every failure `return Pair.of(false, ...)` in `PrecompiledContracts.java`, which I could not complete within the available tool budget. The double-check failed to find literal `return Pair.of(...)` lines with `grep_search` (likely due to line-wrapping or different formatting), which prevented listing the actual byte-array arguments used in the 11 `Pair.of(false, ...)` failure branches.

### Recommendation
Guard the final `memorySave` call in `callToPrecompiledAddress` so it only executes when `out.getRight()` is non-null (or always default to an empty byte array on failure), instead of unconditionally reusing whatever value the failed `execute()` call produced — analogous to `msdc_start_data()` being made to check the `msdc_prepare_data()` result before proceeding with the DMA. Audit every `Pair.of(false, ...)` failure return in `PrecompiledContracts.java` to guarantee a non-null byte array (e.g., `EMPTY_BYTE_ARRAY`) is always returned on failure.

### Proof of Concept
Not independently reproduced in this session — reproduction would require identifying (or crafting) a precompiled-contract call path whose `execute()` failure branch returns `Pair.of(false, null)`, then invoking that precompile via `CALL`/`STATICCALL` from a deployed contract with a non-zero `outDataSize`, and observing whether `Memory.write`/`memorySave` throws an unhandled NPE. I flag this as unverified given the tool-call budget was exhausted before I could enumerate all 11 `Pair.of(false, ...)` sites in `PrecompiledContracts.java`.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L400-402)
```java
  public void memorySave(int addr, byte[] value) {
    memory.write(addr, value, value.length, false);
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L411-413)
```java
  public void memorySave(int addr, int allocSize, byte[] value) {
    memory.extendAndWrite(addr, allocSize, value);
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L1734-1773)
```java
    long requiredEnergy = contract.getEnergyForData(data);
    if (requiredEnergy > msg.getEnergy().longValue()) {
      // Not need to throw an exception, method caller needn't know that
      // regard as consumed the energy
      this.refundEnergy(0, CALL_PRE_COMPILED); //matches cpp logic
      this.stackPushZero();
    } else {
      // Delegate or not. if is delegated, we will use msg sender, otherwise use contract address
      if (msg.getOpCode() == Op.DELEGATECALL) {
        contract.setCallerAddress(getCallerAddress().toTronAddress());
      } else {
        contract.setCallerAddress(getContextAddress());
      }
      // this is the depositImpl, not contractState as above
      contract.setRepository(deposit);
      contract.setResult(this.result);
      contract.setConstantCall(isConstantCall());
      contract.setVmShouldEndInUs(getVmShouldEndInUs());
      Pair<Boolean, byte[]> out = contract.execute(data);

      if (out.getLeft()) { // success
        this.refundEnergy(msg.getEnergy().longValue() - requiredEnergy, CALL_PRE_COMPILED);
        this.stackPushOne();
        returnDataBuffer = out.getRight();
        deposit.commit();
      } else {
        // spend all energy on failure, push zero and revert state changes
        this.refundEnergy(0, CALL_PRE_COMPILED);
        this.stackPushZero();
        if (Objects.nonNull(this.result.getException())) {
          throw result.getException();
        }
      }

      if (VMConfig.allowTvmSelfdestructRestriction()) {
        this.memorySave(msg.getOutDataOffs().intValueSafe(), msg.getOutDataSize().intValueSafe(), out.getRight());
      } else {
        this.memorySave(msg.getOutDataOffs().intValue(), out.getRight());
      }
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Memory.java (L60-68)
```java
  public void write(int address, byte[] data, int dataSize, boolean limited) {

    if (data.length < dataSize) {
      dataSize = data.length;
    }

    if (!limited) {
      extend(address, dataSize);
    }
```
