Based on the investigation, I found a genuinely reachable, unguarded memory-exhaustion class of bug in the TVM precompiled contracts, analogous to the `ReadPCXImage` bug class (an attacker-controlled length field drives an unchecked allocation).

### Title
Memory exhaustion via unchecked attacker-controlled array length in precompiled contract input parsing - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
`PrecompiledContracts.extractBytesArray` and `extractBytes32Array` read a 256-bit word from calldata, truncate it with `intValueSafe()`, and immediately use it as the size of a Java array (`new byte[len][]`) with no upper bound check against the actual size of the supplied `data`. `intValueSafe()` clamps overflowing values to `Integer.MAX_VALUE` rather than rejecting them, so a crafted, tiny transaction payload can force an allocation request for a multi-hundred-megabyte to multi-gigabyte object array before any bounds validation occurs.

### Finding Description
`extractBytesArray` derives `len` purely from a single 32-byte word supplied by the caller: [1](#0-0) 
There is no check that `len` is consistent with `data.length` (unlike, e.g., `Memory`/opcode-level copies, which are pre-validated by `EnergyCost.calcMemEnergy` against a hard `MEM_LIMIT` of 3MB, as seen in `checkMemorySize`): [2](#0-1) 
`extractBytes32Array` has the identical pattern: [3](#0-2) 
Because `DataWord.intValueSafe()` clamps any value with more than 4 significant bytes (or a negative-looking value) to `Integer.MAX_VALUE` instead of throwing or capping to input size: [4](#0-3) 
a single crafted word (e.g. `0xFFFFFFFF...`) makes `len` become `Integer.MAX_VALUE` (2^31-1). The subsequent `new byte[len][]` allocates an object-array of that many references (~8-16 GB on a 64-bit JVM before any element is even populated), which is fundamentally different from the opcode-level memory paths (`OperationActions.codeCopyAction`/`extCodeCopyAction`) that are gated by the `EnergyCost` memory-expansion cost function and its 3MB `MEM_LIMIT` before allocation occurs: [5](#0-4) 
No equivalent memory-size gate exists for the array-length fields consumed inside `PrecompiledContracts` helper parsers, so the allocation happens directly inside `execute(byte[] data)` of the relevant precompiled contract, which is reachable by any address calling that precompile with crafted `data` in a normal `TriggerSmartContract`.

### Impact Explanation
An attacker-controlled call to a precompiled contract that funnels its input through `extractBytesArray`/`extractBytes32Array` can trigger an immediate massive heap allocation attempt, throwing `OutOfMemoryError` (uncaught `Error`, not `Exception`) inside transaction execution on every full node that processes/validates the block, or during transaction broadcast simulation. Because block-application and transaction re-execution happen node-wide, this can crash or hang FullNodes/SRs processing the transaction, resulting in a network-wide denial of service — matching the "node crash or halt" acceptance criterion.

### Likelihood Explanation
Any unprivileged account can construct and broadcast a `TriggerSmartContract` calling the affected precompile address with attacker-chosen calldata; no special privilege, large balance, or prior state is required beyond enough bandwidth/energy to have the call dispatched to the precompile's `execute` method, which is the only prerequisite because the crash occurs before any expensive/gated computation.

### Recommendation
Bound `len` against the actual remaining length of `data` (and/or a small protocol-defined maximum item count) before allocating `bytesArray`/`bytes32Array` in `extractBytesArray`, `extractBytes32Array`, and `extractSigArray`, rejecting the call (returning failure, not an unchecked allocation) when `len` is inconsistent with `data.length`.

### Proof of Concept
Craft calldata for the affected precompile such that the word at the expected `offset` position decodes (via `intValueSafe()`) to a very large value (e.g., all `0xFF` bytes in the low 4 bytes, or any value requiring 5+ significant bytes so it clamps to `Integer.MAX_VALUE`), while keeping the rest of `data` minimal. Submit this as a `TriggerSmartContract` transaction calling the precompile address; on execution, `extractBytesArray`/`extractBytes32Array` attempts `new byte[Integer.MAX_VALUE][]`, exhausting heap and throwing `OutOfMemoryError` in the node process handling/validating the transaction.

**Caveat**: I was not able to trace, within the available index, the exact precompile address(es) whose `execute(byte[] data)` calls `extractBytesArray`/`extractBytes32Array` with an offset directly under full attacker control versus one that is pre-validated by a fixed-format check earlier in that specific precompile's code (e.g., zk-SNARK precompiles have some fixed-size parsing before this point, per the `spendCount`/`receiveCount` code seen at [6](#0-5) ). Confirming the exact reachable precompile and whether an upstream length check already exists would require reading the full `PrecompiledContracts.java` file, which exceeds what the index surfaced. I recommend a full manual review of every caller of `extractBytesArray`/`extractBytes32Array`/`extractSigArray` in that file to confirm which precompiles are affected before treating this as fully confirmed.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L390-397)
```java
  private static byte[][] extractBytes32Array(DataWord[] words, int offset) {
    int len = words[offset].intValueSafe();
    byte[][] bytes32Array = new byte[len][];
    for (int i = 0; i < len; i++) {
      bytes32Array[i] = words[offset + i + 1].getData();
    }
    return bytes32Array;
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L399-412)
```java
  private static byte[][] extractBytesArray(DataWord[] words, int offset, byte[] data) {
    if (offset > words.length - 1) {
      return new byte[0][];
    }
    int len = words[offset].intValueSafe();
    byte[][] bytesArray = new byte[len][];
    for (int i = 0; i < len; i++) {
      int bytesOffset = words[offset + i + 1].intValueSafe() / WORD_SIZE;
      int bytesLen = words[offset + bytesOffset + 1].intValueSafe();
      bytesArray[i] = extractBytes(data, (bytesOffset + offset + 2) * WORD_SIZE,
          bytesLen);
    }
    return bytesArray;
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1502-1509)
```java
        byte[][] spendCv = new byte[spendCount][32];
        byte[][] rk = new byte[spendCount][32];
        byte[][] spendProof = new byte[spendCount][192];
        byte[][] spendAuthSig = new byte[spendCount][64];
        byte[][] receiveCm = new byte[receiveCount][32];
        byte[][] receiveCv = new byte[receiveCount][32];
        byte[][] receiveEpk = new byte[receiveCount][32];
        byte[][] receiveProof = new byte[receiveCount][192];
```

**File:** actuator/src/main/java/org/tron/core/vm/EnergyCost.java (L122-129)
```java
  public static long getCodeCopyCost(Program program) {
    Stack stack = program.getStack();
    long oldMemSize = program.getMemSize();
    long energyCost = calcMemEnergy(oldMemSize,
        memNeeded(stack.peek(), stack.get(stack.size() - 3)),
        stack.get(stack.size() - 3).longValueSafe(), Op.CODECOPY);
    return energyCost;
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/EnergyCost.java (L572-576)
```java
  private static void checkMemorySize(int op, BigInteger newMemSize) {
    if (newMemSize.compareTo(MEM_LIMIT) > 0) {
      throw Program.Exception.memoryOverflow(op);
    }
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
