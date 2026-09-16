## Title
Unbounded array-length allocation in TVM precompiled contract input parsing can crash the node - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
The reported OneFlow CVE-2025-71011 is a classic "attacker-controlled size value used to allocate memory without an upper bound check" DoS bug. The same anti-pattern exists in java-tron's TVM precompiled-contract call-data parsers `extractBytes32Array` and `extractBytesArray`, which take an array length directly from attacker-supplied `DataWord` call data and use it to allocate a Java array with no maximum-size validation, unlike sibling precompiles (`ModExp`, `VerifyTransferProof`) that explicitly bound similar length fields.

### Finding Description
`extractBytes32Array` and `extractBytesArray` read a 32-byte length word from the precompile's raw call-data and immediately use it to size a `byte[][]` allocation: [1](#0-0) 

`len` comes from `words[offset].intValueSafe()`, i.e. directly attacker-controlled via the call data passed to a precompiled-contract invocation (e.g. `CALL`/`STATICCALL` to the batch signature verification precompile), with no comparison against a sane maximum before `new byte[len][]` / `new byte[len][32]` is executed. This is in sharp contrast to the two other precompiles in the same file that parse similarly-shaped length fields and explicitly bound them: `ModExp` rejects any `baseLen/expLen/modLen > UPPER_BOUND (1024)` before use [2](#0-1) , and `VerifyTransferProof` rejects `spendCount`/`receiveCount` outside `[1,2]` before allocating any arrays [3](#0-2) .

`extractBytes32Array`/`extractBytesArray` have no equivalent guard, so a crafted length word (up to `Integer.MAX_VALUE`, since `intValueSafe()` clamps but does not reject) triggers allocation of an oversized array, which will throw `OutOfMemoryError` (or `NegativeArraySizeException`/`OutOfMemoryError` depending on JVM heap state) inside the actuator/VM execution path used by every node validating the transaction.

### Impact Explanation
`OutOfMemoryError` thrown deep inside precompiled-contract execution is a JVM `Error`, not a checked `Exception`; whether it is caught depends on the surrounding TVM dispatch code. Java-tron elsewhere explicitly wraps VM memory-expansion overflow into a bounded, catchable `Program.OutOfMemoryException` (see the `EnergyCost`/`Memory` overflow-check tests) precisely to avoid an uncaught `OutOfMemoryError` propagating out of contract execution [4](#0-3) . `extractBytes32Array`/`extractBytesArray` bypass that controlled path entirely, allocating raw Java arrays with no size ceiling. If the resulting `OutOfMemoryError` is not caught by a generic `Throwable` handler somewhere up the call stack, it can crash the executing thread/node process during block application, since every full node re-executes the same transaction deterministically — a single crafted transaction could be broadcast and cause repeated crashes across the network (chain halt), which matches the "node crash or halt" impact bar in this program's scope.

### Likelihood Explanation
Any unprivileged transaction broadcaster can trigger this: TVM precompiled contracts are reachable from `TriggerSmartContractActuator`-driven `CALL`/`STATICCALL` opcodes without special permissions. No fee-based or size-based gate is visible in the two helper methods themselves; the guard, if any, must exist in each precompile's caller. I was not able to fully confirm within available context whether the specific precompile(s) that invoke `extractBytes32Array`/`extractBytesArray` impose their own upper bound on the length word before calling these helpers (e.g., the batch-signature-verification precompile's `cnt`/`addresses`/`signatures` construction was only partially visible in the available index). This is the key open question that determines whether the bug is directly exploitable or already mitigated by a caller-side check.

### Recommendation
Add an explicit maximum-length check (mirroring `ModExp.UPPER_BOUND` or `VerifyTransferProof`'s spend/receive count bounds) in `extractBytes32Array` and `extractBytesArray` before allocating `byte[len][]`, and/or ensure any caught `Throwable`/`Error` in the TVM precompile dispatch path safely reverts the call rather than propagating an `OutOfMemoryError` out of block processing.

### Proof of Concept
Not independently reproduced — this is an analog derived from static code inspection of `extractBytes32Array`/`extractBytesArray` versus the bounded siblings `ModExp`/`VerifyTransferProof`. Concretely reproducing the crash requires identifying which precompiled contract(s) call these two unbounded helpers and confirming the caller does not itself validate the length word, which the available index did not fully expose. [1](#0-0)

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L390-412)
```java
  private static byte[][] extractBytes32Array(DataWord[] words, int offset) {
    int len = words[offset].intValueSafe();
    byte[][] bytes32Array = new byte[len][];
    for (int i = 0; i < len; i++) {
      bytes32Array[i] = words[offset + i + 1].getData();
    }
    return bytes32Array;
  }

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L693-700)
```java
      int baseLen = parseLen(data, 0);
      int expLen = parseLen(data, 1);
      int modLen = parseLen(data, 2);

      if (VMConfig.allowTvmOsaka()
          && (baseLen > UPPER_BOUND || expLen > UPPER_BOUND || modLen > UPPER_BOUND)) {
        return Pair.of(false, EMPTY_BYTE_ARRAY);
      }
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1492-1499)
```java
        int spendCount = parseInt(data, spendOffset);
        int spendAuthSigCount = parseInt(data, spendAuthSigOffset);
        int receiveCount = parseInt(data, receiveOffset);

        if (spendCount != spendAuthSigCount || spendCount < 1
            || spendCount > 2 || receiveCount < 1 || receiveCount > 2) {
          return Pair.of(true, DataWord.ZERO().getData());
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
