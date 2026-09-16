### Title
Unbounded array allocation from attacker-controlled length field in TVM precompile ABI decoding leads to node crash (DoS) - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
CVE-2025-29482 describes a buffer overflow in libheif/libde265 caused by insufficient bounds validation of an attacker-controlled size/offset field before it is used to allocate/index a buffer during SAO processing. The closest reachable analog in java-tron is the ABI-decoding helper methods `extractSigArray` and `extractBytesArray` in `PrecompiledContracts.java`, which read a length field directly out of calldata (`words[offset].intValueSafe()`) and use it, unvalidated, to size a Java array and to index further into the `words[]` array, before any check that the value is sane relative to the actual calldata size.

### Finding Description
`extractBytesArray` and `extractSigArray` both parse ABI-encoded arrays from a `DataWord[] words` buffer derived from precompiled-contract calldata: [1](#0-0) [2](#0-1) 

In both methods, `len` is read directly from attacker-controlled input (`words[offset].intValueSafe()`) with no upper-bound check against the actual size of `words` or `data` before it is used to allocate `new byte[len][]` and to drive a loop that indexes further into `words[offset + i + 1]`. If `len` is crafted to be very large (up to `Integer.MAX_VALUE`), the array allocation can trigger an `OutOfMemoryError`; if `len` is crafted to exceed the real length of `words`, the loop will throw an unhandled `ArrayIndexOutOfBoundsException`. This mirrors the CVE's root cause — trusting an attacker-supplied size/offset field to drive a buffer allocation/copy operation without validating it against the actual data bounds.

### Impact Explanation
Precompiled contracts that use these ABI-decoding helpers (e.g., the multi-signature verification precompiles such as `ValidateMultiSign`/`BatchValidateSign`, reachable at `validateMultiSignAddr`/`batchValidateSignAddr` via `getContractForAddress`) are invocable by any TVM contract call from an unprivileged transaction. A crafted `len` value can force an `OutOfMemoryError` or uncaught `ArrayIndexOutOfBoundsException` during transaction execution. Depending on how these exceptions propagate through the VM/actuator execution path, this can crash the node process or the validating node's block-processing thread, which falls squarely into the accepted "node crash or halt" impact category. [3](#0-2) 

### Likelihood Explanation
Likelihood is limited by two factors I could not fully verify within this pass: (1) I was unable to confirm the exact call site(s) that invoke `extractSigArray`/`extractBytesArray` from a specific precompile's `execute(byte[] data)` method (my searches only located the helper definitions, not confirmed direct callers within the executes for `ValidateMultiSign`/`BatchValidateSign`); and (2) whether the TVM/actuator wraps precompile execution in a broad try/catch that would turn any runtime exception into a normal "call reverted" outcome rather than propagating to a crash. Given `PrecompiledContract.execute` is invoked from the VM interpreter loop, most TVM implementations catch `Throwable` at the call-frame level to convert failures into reverts, which would reduce this to a gas-wasted revert rather than a crash — this significantly lowers confidence in the "node crash" impact without further evidence.

### Recommendation
Add explicit bounds validation immediately after reading `len` in `extractBytesArray` and `extractSigArray`: reject if `len < 0`, if `len` exceeds a sane maximum (e.g., derived from `data.length / WORD_SIZE`), and if `offset + len + 1` exceeds `words.length` before entering the loop, returning an empty array or failing the precompile call cleanly instead of allocating or indexing based on unchecked attacker input.

### Proof of Concept
Not confirmed. I was unable to trace, within the available tool budget, a concrete call path from a specific precompile's `execute(byte[] data)` entry point through to `extractSigArray`/`extractBytesArray`, nor whether the TVM interpreter's precompile-invocation frame catches unbounded `OutOfMemoryError`/`ArrayIndexOutOfBoundsException` before it can affect node stability. Without that confirmation, I cannot demonstrate a concrete crafted-calldata PoC that reaches a crash rather than a simple reverted transaction.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L254-259)
```java
    if (VMConfig.allowTvmSolidity059() && address.equals(batchValidateSignAddr)) {
      return batchValidateSign;
    }
    if (VMConfig.allowTvmSolidity059() && address.equals(validateMultiSignAddr)) {
      return validateMultiSign;
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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L414-426)
```java
  private static byte[][] extractSigArray(DataWord[] words, int offset, byte[] data) {
    if (offset > words.length - 1) {
      return new byte[0][];
    }
    int len = words[offset].intValueSafe();
    byte[][] bytesArray = new byte[len][];
    for (int i = 0; i < len; i++) {
      int bytesOffset = words[offset + i + 1].intValueSafe() / WORD_SIZE;
      bytesArray[i] = extractBytes(data, (bytesOffset + offset + 2) * WORD_SIZE,
          SIG_LENGTH);
    }
    return bytesArray;
  }
```
