### Title
Unbounded index computation in TVM signature-batch precompiles causes uncaught runtime exception during transaction execution - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
The external CVE describes a class of bugs where a protocol parser trusts an attacker-controlled length/offset field and copies/reads memory without validating it against the buffer bounds, producing out-of-bounds access. The java-tron TVM precompiled-contract helpers `extractBytesArray` and `extractSigArray` in `PrecompiledContracts.java` exhibit the same root-cause pattern: they read length and offset fields directly from caller-supplied `DataWord[]` call data and use them to index into a fixed-size `data` byte array with no bounds validation before the derived index/length is used in `Arrays.copyOfRange`.

### Finding Description
`extractBytesArray` and `extractSigArray` compute `bytesOffset` and `bytesLen` purely from attacker-controlled `words[]` values (`words[offset + i + 1].intValueSafe()`), then call `extractBytes`, which does: [1](#0-0) 
`extractBytes` performs `Arrays.copyOfRange(data, offset, offset + len)` with no check that `offset >= 0`, `offset + len <= data.length`, or that `offset + len` does not overflow `int`. Because `bytesOffset` and `bytesLen` are derived from fully attacker-controlled call data (any contract can invoke these precompiles via `CALL`/`STATICCALL` with crafted ABI-encoded input), a malicious contract can supply values that make `offset` negative or `offset + len` exceed `data.length`, or overflow to a negative sum. `Arrays.copyOfRange` in that case throws `ArrayIndexOutOfBoundsException` / `NegativeArraySizeException`, which is a `RuntimeException` — the same class of "insufficient bounds validation before buffer access" bug as the tcpdump parsers described in the report (missing length check before touching the buffer), just manifesting as a Java exception instead of a memory-safety violation.

### Impact Explanation
If this exception is not caught somewhere in the VM's contract-call dispatch path and propagated up through `Program`/`Runtime` execution, it can turn what should be a graceful "invalid input → revert" into an unhandled exception during block/transaction processing, which is a `Critical`-class node-availability issue (chain halt / node crash) analogous in severity to the CVE's classification (buffer overflow via insufficient parser bounds checking). However, I was not able to confirm within the available context that the outer TVM dispatcher (`Program`/precompile invocation wrapper) fails to catch general `RuntimeException` from `PrecompiledContract.execute()`; if it is caught and safely converted into an EVM revert, the practical impact is limited to a failed/reverted call rather than a node crash.

### Likelihood Explanation
Any account can broadcast a `TriggerSmartContract` transaction calling a contract that invokes one of the precompiles that use `extractBytesArray`/`extractSigArray` (this includes the multi-signature validation precompiles referenced in `BatchValidateSignContractTest.java` / `ValidateMultiSignContractTest.java`), supplying crafted call data with out-of-range offset/length words. This requires no privileges beyond deploying/calling a contract, making the trigger trivially reachable by any unprivileged transaction broadcaster.

### Recommendation
Add explicit bounds validation in `extractBytes`/`extractBytesArray`/`extractSigArray` before computing offsets and slicing: verify `bytesOffset >= 0`, `bytesLen >= 0`, and `offset + len <= data.length` (checked without integer overflow, e.g. using `Math.addExact`/long arithmetic) prior to calling `Arrays.copyOfRange`, returning an empty/failed result instead of throwing when the input is malformed.

### Proof of Concept
I could not construct a concrete, verifiable end-to-end PoC (exact opcode/ABI byte sequence and confirmation that the resulting exception escapes uncaught) within the available index/context — this would require tracing the full call path from `Program`'s `CALL` opcode handler through to `PrecompiledContracts.execute()` and its exception handling, which I could not fully verify with the tools available. [2](#0-1)

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L399-430)
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

  private static byte[] extractBytes(byte[] data, int offset, int len) {
    return Arrays.copyOfRange(data, offset, offset + len);
  }
```
