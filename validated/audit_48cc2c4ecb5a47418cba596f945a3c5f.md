### Title
Unsafe truncating downcast in `DataWord.intValue()` used unguarded for MSTORE/MLOAD memory addressing in the TVM - ([File: common/src/main/java/org/tron/common/runtime/vm/DataWord.java])

### Summary
`DataWord.intValue()` converts a full 256-bit EVM word to a Java `int` by repeatedly shifting and OR-ing every byte of the 32-byte word, silently discarding the high-order bytes. Its own Javadoc claims it "throws ArithmeticException if this will not fit in an int," but the implementation performs no such check — it is a pure truncating downcast, exactly the bug class described in the reference report. This unchecked conversion is used directly (not through the safe `intValueSafe()` variant) to compute memory offsets for the `MSTORE`/`MLOAD` opcodes in the TVM.

### Finding Description
`DataWord.intValue()`:
```java
public int intValue() {
  int intVal = 0;
  for (byte aData : data) {
    intVal = (intVal << 8) + (aData & 0xff);
  }
  return intVal;
}
``` [1](#0-0) 

is documented to throw `ArithmeticException` on overflow, but it never checks range; a value like `2^32 + 4` truncates to `4`. The codebase itself demonstrates the correct/safe alternative right next to it, `intValueSafe()`, which checks `bytesOccupied() > 4` and clamps to `Integer.MAX_VALUE` instead of wrapping: [2](#0-1) 

In `Program.java`, the memory-access helpers used for opcode execution call the **unsafe** `intValue()` directly on attacker-controlled stack `DataWord`s, with no upstream bounds validation:
```java
public void memorySave(DataWord addrB, DataWord value) {
  memory.write(addrB.intValue(), value.getData(), value.getData().length, false);
}
...
public void memoryExpand(DataWord outDataOffs, DataWord outDataSize) {
  if (!outDataSize.isZero()) {
    memory.extend(outDataOffs.intValue(), outDataSize.intValue());
  }
}
public DataWord memoryLoad(DataWord addr) {
  return memory.readWord(addr.intValue());
}
``` [3](#0-2) 

These are invoked directly by the `MSTORE`/`MLOAD` opcode handlers, which pop the raw stack `DataWord` and pass it straight through without any range check:
```java
public static void mLoadAction(Program program) {
  DataWord addr = program.stackPop();
  DataWord data = program.memoryLoad(addr);
  program.stackPush(data);
  program.step();
}

public static void mStoreAction(Program program) {
  DataWord addr = program.stackPop();
  DataWord value = program.stackPop();
  program.memorySave(addr, value);
  program.step();
}
``` [4](#0-3) 

By contrast, essentially every *other* memory-touching opcode in the same file (`SHA3`, `CALLDATACOPY`, `CODECOPY`, `EXTCODECOPY`, `RETURNDATACOPY`, `MSTORE8`) is careful to call `.intValueSafe()` before using an offset as a Java `int`: [5](#0-4) [6](#0-5) 

This shows the codebase's own convention: any raw `DataWord` offset that reaches Java-level memory addressing must be passed through the safe, clamping conversion. `MSTORE`/`MLOAD` (via `Program.memorySave(DataWord, DataWord)` / `memoryLoad(DataWord)`) break that convention and rely on the raw, truncating `intValue()`.

### Impact Explanation
Any contract deployer or transaction sender who can get code containing `MSTORE`/`MLOAD` executed (i.e., any TVM contract invocation, which is reachable by an unprivileged, unpermissioned account) controls the 256-bit stack value used as the memory offset. Because `intValue()` truncates instead of throwing or safely clamping (as `intValueSafe()` does elsewhere), a crafted huge offset (e.g. `2^32 + N`) is silently reinterpreted as offset `N mod 2^32`. This breaks the invariant the rest of the interpreter relies on — that an offset either fits safely in an `int` or is clamped/rejected — and creates a discrepancy between the address a contract author/auditor believes is being read/written (a huge, presumably out-of-range value) and the address that is actually touched in the node's in-memory byte buffer. Depending on how memory-extension gas accounting for `MSTORE`/`MLOAD` treats the same value elsewhere in the pipeline, this class of inconsistency can lead to either unmetered/undercharged memory access or unexpected read/write collisions inside the VM's memory buffer during contract execution — a node-level correctness/DoS-class issue reachable purely from a signed transaction invoking arbitrary bytecode.

I was not able to fully verify, in the time available, whether the `MSTORE`/`MLOAD` energy/gas cost pre-check (`EnergyCost` / `memNeeded` / `calcMemEnergy`) computes memory-extension size using the same unsafe `intValue()` or the safe/clamped path, so I cannot conclusively confirm whether this specific truncation can be used to fully bypass gas metering or only produces an internal inconsistency. This should be verified against `EnergyCost.calcMemEnergy`/`memNeeded` before assuming full exploitability.

### Likelihood Explanation
High reachability: `MSTORE`/`MLOAD` are among the most common EVM opcodes, and any deployed contract that a user calls can trigger this path with attacker-chosen stack values with no special privilege required. The only uncertainty is the severity ceiling (metering bypass vs. internal inconsistency/crash), not the reachability of the unsafe cast itself.

### Recommendation
Replace the direct `addr.intValue()` / `addrB.intValue()` / `outDataOffs.intValue()` / `outDataSize.intValue()` calls in `Program.memorySave(DataWord, DataWord)`, `Program.memoryLoad(DataWord)`, and `Program.memoryExpand(DataWord, DataWord)` with the existing `intValueSafe()` (or an equivalent checked conversion that throws/aborts execution on overflow rather than silently truncating), matching the pattern already used by `SHA3`, `CALLDATACOPY`, `CODECOPY`, `EXTCODECOPY`, and `MSTORE8`. Additionally, fix or remove the misleading Javadoc on `DataWord.intValue()` that claims range-checking behavior it does not implement, to prevent other unsafe call sites from being introduced under a false safety assumption.

### Proof of Concept
1. Deploy any contract whose bytecode executes `MSTORE` (or `MLOAD`) with an offset word equal to `0x1_00000004` (i.e., `2^32 + 4`), e.g. via inline assembly: `mstore(0x100000004, 0x1234)`.
2. Call the contract function through a normal signed transaction (no special privileges needed).
3. Trace execution into `OperationActions.mStoreAction` → `Program.memorySave(DataWord, DataWord)` → `memory.write(addrB.intValue(), ...)`.
4. Observe that `addrB.intValue()` returns `4` (the low 32 bits of the 33-bit value) instead of throwing `ArithmeticException` as its Javadoc promises, or being clamped to `Integer.MAX_VALUE` as `intValueSafe()` would do — causing the write to land at offset `4` in the VM's memory buffer rather than being rejected or safely bounded.

### Citations

**File:** common/src/main/java/org/tron/common/runtime/vm/DataWord.java (L202-217)
```java
  /**
   * Converts this DataWord to an int, checking for lost information. If this DataWord is out of the
   * possible range for an int result then an ArithmeticException is thrown.
   *
   * @return this DataWord converted to an int.
   * @throws ArithmeticException - if this will not fit in an int.
   */
  public int intValue() {
    int intVal = 0;

    for (byte aData : data) {
      intVal = (intVal << 8) + (aData & 0xff);
    }

    return intVal;
  }
```

**File:** common/src/main/java/org/tron/common/runtime/vm/DataWord.java (L219-229)
```java
  /**
   * In case of int overflow returns Integer.MAX_VALUE otherwise works as #intValue()
   */
  public int intValueSafe() {
    int bytesOccupied = bytesOccupied();
    int intValue = intValue();
    if (bytesOccupied > 4 || intValue < 0) {
      return Integer.MAX_VALUE;
    }
    return intValue;
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L396-431)
```java
  public void memorySave(DataWord addrB, DataWord value) {
    memory.write(addrB.intValue(), value.getData(), value.getData().length, false);
  }

  public void memorySave(int addr, byte[] value) {
    memory.write(addr, value, value.length, false);
  }

  /**
   * . Allocates a piece of memory and stores value at given offset address
   *
   * @param addr is the offset address
   * @param allocSize size of memory needed to write
   * @param value the data to write to memory
   */
  public void memorySave(int addr, int allocSize, byte[] value) {
    memory.extendAndWrite(addr, allocSize, value);
  }

  public void memorySaveLimited(int addr, byte[] data, int dataSize) {
    memory.write(addr, data, dataSize, true);
  }

  public void memoryExpand(DataWord outDataOffs, DataWord outDataSize) {
    if (!outDataSize.isZero()) {
      memory.extend(outDataOffs.intValue(), outDataSize.intValue());
    }
  }

  public DataWord memoryLoad(DataWord addr) {
    return memory.readWord(addr.intValue());
  }

  public DataWord memoryLoad(int address) {
    return memory.readWord(address);
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/OperationActions.java (L302-313)
```java
  public static void sha3Action(Program program) {
    DataWord memOffsetData = program.stackPop();
    DataWord lengthData = program.stackPop();
    byte[] buffer = program
        .memoryChunk(memOffsetData.intValueSafe(), lengthData.intValueSafe());

    byte[] encoded = sha3(buffer);
    DataWord word = new DataWord(encoded);

    program.stackPush(word);
    program.step();
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/OperationActions.java (L563-577)
```java
  public static void mLoadAction(Program program) {
    DataWord addr = program.stackPop();
    DataWord data = program.memoryLoad(addr);

    program.stackPush(data);
    program.step();
  }

  public static void mStoreAction(Program program) {
    DataWord addr = program.stackPop();
    DataWord value = program.stackPop();

    program.memorySave(addr, value);
    program.step();
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/OperationActions.java (L579-586)
```java
  public static void mStore8Action(Program program) {
    DataWord addr = program.stackPop();
    DataWord value = program.stackPop();

    byte[] byteVal = {value.getData()[31]};
    program.memorySave(addr.intValueSafe(), byteVal);
    program.step();
  }
```
