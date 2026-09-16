### Title
Precompiled-contract CALL/CALLCODE without `allowTvmSelfdestructRestriction` copies unbounded return data into memory beyond the requested `outDataSize`, letting an attacker grief energy accounting - (File: `actuator/src/main/java/org/tron/core/vm/program/Program.java`)

### Summary
`Program.callToPrecompiledAddress` writes the entire byte array returned by a precompiled contract's `execute(data)` into the caller's memory via the *unlimited* `memorySave(offset, out.getRight())` overload whenever `VMConfig.allowTvmSelfdestructRestriction()` is not enabled, instead of the caller-specified `outDataSize` that was used earlier to size/charge memory expansion in `exeCall`. [1](#0-0) 

### Finding Description
When a contract executes `CALL`/`CALLCODE`/`STATICCALL`/`DELEGATECALL` against an address that resolves to a precompiled contract, the interpreter first computes the gas/memory cost for the operation using the caller-declared `outDataOffs`/`outDataSize` stack arguments and calls `program.memoryExpand(outDataOffs, outDataSize)` in `OperationActions.exeCall`, charging memory-expansion energy only for that declared size. [2](#0-1) 

However, once the precompiled contract actually executes and returns its output (`out.getRight()`), `Program.callToPrecompiledAddress` writes that output into memory using two different code paths depending on a feature flag:
```java
if (VMConfig.allowTvmSelfdestructRestriction()) {
  this.memorySave(msg.getOutDataOffs().intValueSafe(), msg.getOutDataSize().intValueSafe(), out.getRight());
} else {
  this.memorySave(msg.getOutDataOffs().intValue(), out.getRight());
}
``` [3](#0-2) 

The flag-gated branch correctly bounds the write to `msg.getOutDataSize()` (mirroring the EVM semantics where only the caller-requested number of bytes are copied, analogous to how the report recommends limiting/avoiding automatic returndata copies). The other branch (taken when the flag is not active) calls the unlimited `memorySave(offset, data)` overload, which internally calls `Memory.extend(address, dataSize)` with `dataSize = out.getRight().length` — the *actual* size of the precompiled contract's return value, not the size the calling contract asked for or paid memory-expansion energy for. [4](#0-3) 

This mirrors the root cause in the external report: an external call's return data is written into memory without regard for the amount of memory the caller actually reserved/paid for, so a callee that returns a large amount of data can force uncharged memory growth in the caller's frame. Because `EnergyCost.getCalculateCallCost` (via `calcMemEnergy`) only accounted for `memNeeded(outDataOffs, outDataSize)` at call time using the value on the stack, the extra memory extension triggered inside `callToPrecompiledAddress` for the unbounded branch bypasses the intended energy metering for memory growth, exactly analogous to Pancake V4's `_fetchProtocolFee` copying unbounded returndata without the caller controlling/limiting it. [5](#0-4) 

### Impact Explanation
If a precompiled contract can be induced to return a large byte array (return-size is a function of the precompile's logic, e.g. any of the precompiles whose output size is not fixed/bounded to the requested output window), a contract invoking it with a small `outDataSize` on a chain where `allowTvmSelfdestructRestriction` has not been activated would have memory silently extended by the full unbounded size at no extra declared energy cost from the caller's perspective at call setup time — creating an accounting mismatch between charged memory-expansion energy and actual memory used. Depending on downstream memory-size bookkeeping, this could be leveraged to cause excessive/unbounded memory growth relative to what was paid for, a form of gas/energy griefing against the calling contract and, in aggregate, against node resource accounting during transaction execution.

### Likelihood Explanation
This code path is reached by any unprivileged actor deploying and calling a smart contract that issues `CALL`/`CALLCODE` to a known Tron precompiled-contract address — no special privileges are required, it is directly reachable from a single transaction. The severity is gated by the `allowTvmSelfdestructRestriction` hard-fork flag: if this flag is enabled on the target network, the bounded/safe branch is used and the issue does not manifest, which the java-tron team's own patch pattern (`VMConfig.allowTvmSelfdestructRestriction()`) shows was already partially remediated for this same code path. On networks/configurations where the flag has not yet been activated, or during any historical block range prior to activation, the unbounded branch is live.

### Recommendation
Always use the bounded write path (`memorySave(offset, size, data)`) regardless of `allowTvmSelfdestructRestriction`, so returned data is never written beyond the `outDataSize` the caller declared and paid memory-expansion energy for; retire the unconditional/unlimited `memorySave(offset, data)` branch entirely for precompiled-contract calls.

### Proof of Concept
Conceptual reproduction (cannot be executed without full node/test harness access, but the code paths are cited above):
1. Deploy a contract `A` that performs `CALL` to a precompiled-contract address (e.g., an address in `PrecompiledContracts.getContractForAddress`) with a very small `outDataSize` (e.g., 1 byte) in its call opcode arguments.
2. Choose a precompile whose `execute(data)` implementation can be driven to return output larger than the 1-byte window requested (return size not clamped to `msg.getOutDataSize()` inside `execute`).
3. On a configuration where `VMConfig.allowTvmSelfdestructRestriction()` is disabled, `Program.callToPrecompiledAddress` will call `memorySave(offset, out.getRight())` — the unlimited overload — causing `Memory.extend` to grow memory by the full return size rather than the 1 byte originally charged for during `exeCall`'s `memoryExpand(outDataOffs, outDataSize)` call.
4. Compare the energy charged for the CALL memory expansion (based on the declared `outDataSize`) against the actual amount of VM memory extended after the call returns, confirming the discrepancy described above. [6](#0-5)

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L1734-1774)
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
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/OperationActions.java (L1031-1057)
```java
  public static void exeCall(Program program, DataWord adjustedCallEnergy,
      DataWord codeAddress, DataWord value, DataWord tokenId, boolean isTokenTransferMsg) {

    DataWord inDataOffs = program.stackPop();
    DataWord inDataSize = program.stackPop();

    DataWord outDataOffs = program.stackPop();
    DataWord outDataSize = program.stackPop();

    program.memoryExpand(outDataOffs, outDataSize);
    int op = program.getCurrentOpIntValue();
    MessageCall msg = new MessageCall(
        op, adjustedCallEnergy, codeAddress, value, inDataOffs, inDataSize,
        outDataOffs, outDataSize, tokenId, isTokenTransferMsg);

    PrecompiledContracts.PrecompiledContract contract =
        PrecompiledContracts.getContractForAddress(codeAddress);
    if (contract != null) {
      if (program.isConstantCall()) {
        contract =  PrecompiledContracts.getOptimizedContractForConstant(contract);
      }
      program.callToPrecompiledAddress(msg, contract);
    } else {
      program.callToAddress(msg);
    }
    program.step();
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

**File:** actuator/src/main/java/org/tron/core/vm/EnergyCost.java (L499-511)
```java
  public static long getCalculateCallCost(Stack stack, Program program,
                                          long energyCost, int opOff) {
    int op = program.getCurrentOpIntValue();
    long oldMemSize = program.getMemSize();
    DataWord callEnergyWord = stack.get(stack.size() - 1);
    // in offset+size
    BigInteger in = memNeeded(stack.get(stack.size() - opOff),
        stack.get(stack.size() - opOff - 1));
    // out offset+size
    BigInteger out = memNeeded(stack.get(stack.size() - opOff - 2),
        stack.get(stack.size() - opOff - 3));
    energyCost += calcMemEnergy(oldMemSize, in.max(out),
        0, op);
```
