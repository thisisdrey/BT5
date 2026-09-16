### Title
Integer overflow in `offset + length` bounds check allows OOB read past msgData buffer in CALLDATACOPY - (File: actuator/src/main/java/org/tron/core/vm/program/invoke/ProgramInvokeImpl.java)

### Summary
The CVE describes an integer overflow in an `start_offset + size` bounds computation (`cpia2_remap_buffer`) that lets an unprivileged caller obtain out-of-bounds memory access. The analogous pattern exists in java-tron's TVM `CALLDATACOPY` implementation, where `offset + length` is computed as a plain 32-bit `int` addition without overflow protection before being used to decide how much of `msgData` to copy.

### Finding Description
`ProgramInvokeImpl.getDataCopy(DataWord offsetData, DataWord lengthData)` computes: [1](#0-0) 

```java
int offset = offsetData.intValueSafe();
int length = lengthData.intValueSafe();
byte[] data = new byte[length];
...
if (offset > msgData.length) return data;
if (offset + length > msgData.length) {
  length = msgData.length - offset;
}
System.arraycopy(msgData, offset, data, 0, length);
```
`intValueSafe()` clamps each individual `DataWord` operand to `Integer.MAX_VALUE` when it doesn't fit in 32 bits [2](#0-1) , but the *sum* `offset + length` is still evaluated as a native `int` and can wrap around to a negative value. When it wraps negative, the guard `offset + length > msgData.length` evaluates false even though the true (unwrapped) request greatly exceeds `msgData.length`, so `length` is left unclamped and the subsequent `System.arraycopy` is invoked with the original (unclamped) `length`. This mirrors the CVE's root cause: an offset/size bounds check that overflows and therefore fails to restrict the accessible range, only here it is on the CALLDATACOPY message-data buffer rather than an mmap'd kernel buffer.

By contrast, the sibling `codeCopyAction`/`extCodeCopyAction` in `OperationActions.java` explicitly cast to `long` before adding (`(long) codeOffset + lengthData > fullCode.length`) [3](#0-2) , and `Program.getReturnDataBufferData` does the same for RETURNDATACOPY [4](#0-3) , confirming this class of bug was already recognized and fixed for those two ops but not for `getDataCopy` used by `CALLDATACOPY` / `CALLDATALOAD`.

### Impact Explanation
If reachable, the un-clamped `length` passed to `System.arraycopy(msgData, offset, data, 0, length)` would attempt to read past the end of `msgData`, throwing `ArrayIndexOutOfBoundsException` at best (causing the transaction/contract call to abort/throw, not a controlled memory disclosure) since `System.arraycopy` performs its own bounds checking in the JVM. Unlike the C/kernel case, Java array bounds are enforced by the JVM, so this cannot yield raw memory disclosure or privilege escalation as in the CPIA2 CVE — the practical worst case is an uncaught/mis-handled exception during EVM execution. Since `intValueSafe()` clamps each operand individually and the actual `msgData.length` (calldata size) is bounded by transaction size limits, the specific 32-bit wraparound condition (`offset` up to a few KB, `length` needing to be near 2^31 to wrap) requires `length` to be enormous, which would trigger `new byte[length]` as a huge/failing allocation before the overflowing addition is even reached — in practice `OutOfMemoryError`/allocation failure occurs first.

### Likelihood Explanation
Low-to-moderate. Reaching the actual integer-wraparound branch requires `length` values close to `Integer.MAX_VALUE`, which are already intercepted by the `new byte[length]` allocation (either failing outright or being extremely costly) before the vulnerable `offset + length` comparison is evaluated. This significantly limits real-world exploitability compared to the original CVE, where wraparound directly granted read/write on physical kernel pages.

### Recommendation
Change the bounds check in `ProgramInvokeImpl.getDataCopy` (and the mock counterpart `ProgramInvokeMockImpl.getDataCopy`) to use a widened (`long`) addition, consistent with the fix already applied in `OperationActions.codeCopyAction`/`extCodeCopyAction` and `Program.getReturnDataBufferData`:
```java
if ((long) offset + length > msgData.length) {
  length = msgData.length - offset;
}
```
Also consider bounding `length` before allocating `new byte[length]` to avoid uncontrolled large allocations.

### Proof of Concept
Not independently reproducible as a memory-disclosure/RCE exploit given JVM array bounds enforcement; the overflow condition is real in the arithmetic (`offset + length` wraps for `offset` and `length` near `Integer.MAX_VALUE`) but is gated by the preceding `new byte[length]` allocation, making it primarily a code-correctness/defense-in-depth issue rather than a directly exploitable high-severity bug in this Java context.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/program/invoke/ProgramInvokeImpl.java (L183-203)
```java
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

**File:** actuator/src/main/java/org/tron/core/vm/OperationActions.java (L404-408)
```java

    int sizeToBeCopied = lengthData;
    if ((long) codeOffset + lengthData > fullCode.length) {
      sizeToBeCopied = fullCode.length < codeOffset ? 0 : fullCode.length - codeOffset;
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L1446-1453)
```java
  public byte[] getReturnDataBufferData(DataWord off, DataWord size) {
    if ((long) off.intValueSafe() + size.intValueSafe() > getReturnDataBufferSizeI()) {
      return null;
    }
    return returnDataBuffer == null ? new byte[0] :
        Arrays.copyOfRange(returnDataBuffer, off.intValueSafe(),
            off.intValueSafe() + size.intValueSafe());
  }
```
