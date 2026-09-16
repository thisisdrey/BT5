### Title
Unbounded memory allocation from attacker-controlled length in EXTCODECOPY/CALLDATACOPY before energy metering can crash the TVM (DoS) - (File: actuator/src/main/java/org/tron/core/vm/OperationActions.java)

### Summary
The libtiff report describes a crafted-input length field driving an allocation in `tif_read.c` that aborts the process (memory-allocation-failure DoS). The analogous pattern in java-tron is TVM opcode handlers that take an attacker-controlled 256-bit stack word, convert it to a Java `int`, and immediately allocate a `byte[]` of that size before/independent of proper energy-based memory-expansion accounting, so a single crafted contract call can force a very large heap allocation.

### Finding Description
`extCodeCopyAction` in `OperationActions.java` pops `lengthData` off the EVM/TVM stack and converts it with `intValueSafe()`, then immediately does `byte[] codeCopy = new byte[lengthData];` before any bound is enforced against the actual code size or against a sane memory limit: [1](#0-0) 

The same unguarded allocation pattern occurs in `ProgramInvokeImpl.getDataCopy` (backing `CALLDATACOPY`), where `offset`/`length` come straight from `DataWord.intValueSafe()` on `offsetData`/`lengthData` and a `byte[] data = new byte[length]` is allocated before validating against `msgData.length`: [2](#0-1) 

Normally EVM/TVM implementations charge quadratic memory-expansion gas *before* performing the copy, which should make an attacker pay energy proportional to the requested size and abort with an out-of-gas/`OutOfMemoryException` rather than actually allocating gigabytes of memory. Tests in the repo do show a "cost3"/`checkMemorySize` path in `EnergyCost` designed to catch huge lengths/offsets and throw `Program.OutOfMemoryException` before allocation: [3](#0-2) 

However, I was unable to fully confirm from the available index that every code path reaching `new byte[length]` in `extCodeCopyAction` / `getDataCopy` is preceded by that memory-cost gate for all length ranges (e.g., values close to `Integer.MAX_VALUE` that are large but not large enough to trip a `long` overflow check in `checkMemorySize`). If any call path allows a `length` on the order of hundreds of megabytes to reach the raw `new byte[length]` allocation without first being charged/rejected by the quadratic memory-cost formula, a single crafted transaction that triggers a contract executing `EXTCODECOPY`/`CALLDATACOPY` with such a length can force a large synchronous allocation on the node processing the transaction, mirroring the libtiff "crafted input drives allocation size, causing an abort" bug class. This is a Medium-confidence analog because the exact interaction between `EnergyCost`'s memory-gas gate and these two allocation sites in this snapshot of the code was not fully traceable with the tools available (I could not read `EnergyCost.java`'s full content or `DataWord.intValueSafe()`'s implementation in this session).

### Impact Explanation
If the memory-cost gate does not tightly bound the allocation size relative to available energy, an unprivileged contract deployer or caller could submit a transaction that forces one or more large heap allocations per TVM execution, potentially triggering `OutOfMemoryError` on the executing full node — a node crash/denial-of-service, consistent with the "abort, denial of service" impact in the source report.

### Likelihood Explanation
Low-to-Medium. TVM opcode gas metering is specifically designed to prevent unbounded memory operations, and the presence of dedicated overflow tests (`testLargeArrayLengthOverflow`, `testLargeOffsetOverflow`) suggests the team has already hardened at least the `VoteWitnessCost3`-style paths. Without being able to fully trace `EXTCODECOPY`'s specific gas-cost function and `intValueSafe()`'s exact clamp behavior, I cannot confirm this is currently exploitable — it should be treated as a hypothesis requiring code-level verification rather than a confirmed vulnerability.

### Recommendation
Verify that every `new byte[...]` / `new byte[][]` allocation sized from raw `DataWord`/stack-derived values (`extCodeCopyAction`, `getDataCopy`, `getDataValue`, and similar opcode handlers) is preceded by an energy/memory-expansion charge computed with overflow-safe arithmetic (as already implemented for `VOTE`-family costs via `checkMemorySize`), and that the charge is applied for the full requested length, not just the portion actually copied. Add explicit upper bounds or catch-and-convert `OutOfMemoryError` into `Program.OutOfMemoryException` at these specific allocation sites as defense-in-depth.

### Proof of Concept
Conceptual: A contract executing `EXTCODECOPY(address, memOffset, codeOffset, length)` with `length` set to a very large value (e.g., near `Integer.MAX_VALUE`) would hit line 476 of `OperationActions.java` (`byte[] codeCopy = new byte[lengthData];`) and attempt to allocate that many bytes regardless of the target contract's actual code size. Confirming exploitability requires tracing the exact `EnergyCost` formula gating this opcode in the current codebase, which I could not complete in this session — a Devin session with full file access would be needed to verify the gas-check ordering and finalize a concrete PoC transaction.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/OperationActions.java (L463-483)
```java
  public static void extCodeCopyAction(Program program) {
    DataWord address = program.stackPop();
    byte[] fullCode = program.getCodeAt(address);

    int memOffset = program.stackPop().intValueSafe();
    int codeOffset = program.stackPop().intValueSafe();
    int lengthData = program.stackPop().intValueSafe();

    int sizeToBeCopied = lengthData;
    if ((long) codeOffset + lengthData > fullCode.length) {
      sizeToBeCopied = fullCode.length < codeOffset ? 0 : fullCode.length - codeOffset;
    }

    byte[] codeCopy = new byte[lengthData];

    if (codeOffset < fullCode.length) {
      System.arraycopy(fullCode, codeOffset, codeCopy, 0, sizeToBeCopied);
    }

    program.memorySave(memOffset, codeCopy);
    program.step();
```

**File:** actuator/src/main/java/org/tron/core/vm/program/invoke/ProgramInvokeImpl.java (L182-203)
```java
  /*  CALLDATACOPY */
  public byte[] getDataCopy(DataWord offsetData, DataWord lengthData) {

    int offset = offsetData.intValueSafe();
    int length = lengthData.intValueSafe();

    byte[] data = new byte[length];

    if (msgData == null) {
      return data;
    }
    if (offset > msgData.length) {
      return data;
    }
    if (offset + length > msgData.length) {
      length = msgData.length - offset;
    }

    System.arraycopy(msgData, offset, data, 0, length);

    return data;
  }
```

**File:** framework/src/test/java/org/tron/common/runtime/vm/VoteWitnessCost3Test.java (L134-157)
```java
  @Test
  public void testLargeArrayLengthOverflow() {
    // Use a very large value that would overflow in DataWord.mul() in cost2
    // DataWord max is 2^256-1, multiplying by 32 would overflow
    // In cost3, BigInteger handles this correctly and should trigger memoryOverflow
    String maxHex = "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff";
    DataWord largeLength = new DataWord(maxHex);
    DataWord zeroOffset = new DataWord(0);

    Program program = mockProgram(zeroOffset, new DataWord(1),
        zeroOffset, largeLength, 0);

    boolean overflowCaught = false;
    VMConfig.initAllowTvmOsaka(1);    // cost3 self-dispatches; exercise the v3 (BigInteger) path
    try {
      EnergyCost.getVoteWitnessCost3(program);
    } catch (Program.OutOfMemoryException e) {
      // cost3 should detect memory overflow via checkMemorySize
      overflowCaught = true;
    } finally {
      VMConfig.initAllowTvmOsaka(0);
    }
    assertTrue("cost3 should throw memoryOverflow for huge array length", overflowCaught);
  }
```
