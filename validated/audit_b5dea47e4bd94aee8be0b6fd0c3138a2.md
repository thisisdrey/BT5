### Title
Unvalidated array-length in `ValidateMultiSign`/`BatchValidateSign` precompiles allows attacker-controlled `new byte[len][]` allocation from calldata - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
The TensorFlow `SparseConcat` bug class is: an untrusted numeric value taken directly from input is used as an array/shape dimension without bounds validation, so a crafted input can force an oversized allocation and abort/crash the process. The same pattern exists in java-tron's `extractBytesArray`/`extractSigArray`/`extractBytes32Array` helpers used by the `ValidateMultiSign` and `BatchValidateSign` TVM precompiles: the array length is read straight from attacker-supplied contract calldata via `DataWord.intValueSafe()` and passed unchecked into `new byte[len][]`.

### Finding Description
`extractBytesArray`, `extractSigArray`, and `extractBytes32Array` compute `len` from a `DataWord` taken from the raw call data and immediately allocate `new byte[len][]`: [1](#0-0) 

These are invoked from `ValidateMultiSign.execute()` and `BatchValidateSign.doExecute()`, which are reachable from any TVM contract call to precompile addresses `0x9` and `0xa` (`validateMultiSignAddr`/`batchValidateSignAddr`), gated only by `VMConfig.allowTvmSolidity059()`: [2](#0-1) 

A size cap (`MAX_SIZE`) is only enforced when the `allowTvmSelfdestructRestriction()` feature flag is active; otherwise `extractBytesArray`/`extractSigArray` is called directly on the attacker-chosen offset with no length check: [3](#0-2) [4](#0-3) 

Because `intValueSafe()` can return values up to `Integer.MAX_VALUE`, an attacker can craft calldata whose length-word is huge, causing `new byte[len][]` to attempt an allocation of billions of array-slot references before any subsequent per-element validation runs. This is analogous to `TensorShape`'s `CHECK`-fail: a raw, attacker-controlled integer is used unchecked as an allocation size.

### Impact Explanation
An unguarded huge-array allocation throws `OutOfMemoryError` (a JVM `Error`, not an `Exception`). The extraction call in `ValidateMultiSign.execute()` (lines 1072–1074) sits outside any try/catch in that method — only the later signature-recovery loop is wrapped in `try { } catch (Throwable t)`. If the `OutOfMemoryError` is thrown before that inner try block, it propagates up through the VM execution stack uncaught, which can crash/destabilize the node process handling the transaction (denial of service), matching the "node crash or halt" acceptance criterion for this class of bug.

### Likelihood Explanation
Reachable by any unprivileged account able to broadcast a TVM contract-call transaction that invokes the `validatemultisign`/`batchvalidatesign` precompile addresses (no special privilege required, and the gating flag `allowTvmSolidity059()` is a long-activated maintenance-level TVM feature). Whether this is currently fully mitigated depends on whether `allowTvmSelfdestructRestriction()` is active on the network being scanned — if it is active, the `MAX_SIZE` check runs before the vulnerable extraction and blocks the attack path; if it is not yet active, the unguarded path is directly reachable.

### Recommendation
Bound `len` in `extractBytesArray`, `extractSigArray`, and `extractBytes32Array` against a fixed maximum (e.g., `MAX_SIZE`) or against the actual number of remaining words in `words`/`data`, before allocating the array, independent of the `allowTvmSelfdestructRestriction()` feature flag. Also wrap precompile length/decoding logic in a `try/catch(Throwable)` (or explicitly catch `OutOfMemoryError`) so that unexpected allocation failures degrade to `Pair.of(false, EMPTY_BYTE_ARRAY)` rather than propagating uncaught.

### Proof of Concept
Craft calldata for `validatemultisign(address,uint256,bytes32,bytes[])` such that the ABI-encoded dynamic-array length word at the signatures-array offset is set to a very large value (e.g., `0x7FFFFFFF`) instead of the true number of encoded signatures, while `VMConfig.allowTvmSelfdestructRestriction()` is not yet enabled. Broadcasting a contract call that triggers this precompile with that calldata causes `extractBytesArray` to execute `new byte[0x7FFFFFFF][]`, exhausting heap and raising an uncaught `OutOfMemoryError` in the transaction-processing path.

*Note: I was unable to fully confirm the exact call-stack behavior of `OutOfMemoryError` propagation through `Program`/`OperationActions` (e.g., whether an outer catch in the VM interpreter loop ultimately absorbs `Throwable`) due to index/search limitations reaching `DataWord.intValueSafe()`'s exact implementation and the full `Program.callToPrecompiledAddress` catch hierarchy. A Devin session with full repository access should verify (a) `DataWord.intValueSafe()`'s clamping behavior and (b) whether any outer `catch (Throwable)` in the VM call path fully absorbs the resulting `OutOfMemoryError` before concluding node-crash severity is confirmed rather than merely a possibility.*

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1066-1074)
```java
      if (VMConfig.allowTvmSelfdestructRestriction()) {
        int sigArraySize = words[words[3].intValueSafe() / WORD_SIZE].intValueSafe();
        if (sigArraySize > MAX_SIZE) {
          return Pair.of(true, DATA_FALSE);
        }
      }
      byte[][] signatures = VMConfig.allowTvmSelfdestructRestriction() ?
          extractSigArray(words, words[3].intValueSafe() / WORD_SIZE, rawData) :
          extractBytesArray(words, words[3].intValueSafe() / WORD_SIZE, rawData);
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1165-1175)
```java
      if (VMConfig.allowTvmSelfdestructRestriction()) {
        int sigArraySize = words[words[1].intValueSafe() / WORD_SIZE].intValueSafe();
        int addrArraySize = words[words[2].intValueSafe() / WORD_SIZE].intValueSafe();
        if (sigArraySize > MAX_SIZE || addrArraySize > MAX_SIZE) {
          return Pair.of(true, DATA_FALSE);
        }
      }

      byte[][] signatures = VMConfig.allowTvmSelfdestructRestriction() ?
          extractSigArray(words, words[1].intValueSafe() / WORD_SIZE, data) :
          extractBytesArray(words, words[1].intValueSafe() / WORD_SIZE, data);
```
