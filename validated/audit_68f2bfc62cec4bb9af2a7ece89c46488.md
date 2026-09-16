### Title
Unbounded array length taken directly from attacker-controlled `DataWord` input causes uncontrolled allocation / out-of-bounds array access in TVM precompiled-contract input decoding - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
`PrecompiledContracts.extractBytesArray` and `extractBytes32Array` decode ABI-encoded dynamic arrays passed as call data to the `BatchValidateSign` (address `0x9`) and `ValidateMultiSign` (address `0xa`) precompiled contracts. Both helpers read an element count directly from a caller-supplied `DataWord` word and then use it, unchecked, to size a new array and to index further into the input `words[]` array, mirroring the class of bug in CVE-2017-5834 where `parse_dict_node` in `bplist.c` trusted an attacker-controlled count/offset from a crafted binary blob and read/allocated out of bounds.

### Finding Description
`extractBytes32Array` reads `len` straight from `words[offset].intValueSafe()` with no upper bound check, then loops `i` from `0` to `len-1` indexing `words[offset + i + 1]`: [1](#0-0) 

`extractBytesArray` only checks that `offset` itself is in range, but `len` (again taken from an attacker-controlled `DataWord`) is used unchecked both to allocate `new byte[len][]` and to index `words[offset + i + 1]` / `words[offset + bytesOffset + 1]`: [2](#0-1) 

Because `words` is a fixed-size array built from the actual call-data length, any `len` value larger than the remaining elements causes `ArrayIndexOutOfBoundsException` while decoding — the direct analog of the OOB heap read in `parse_dict_node`. Additionally, a large `len` (up to `Integer.MAX_VALUE`, since `intValueSafe()` only clamps to `int` range) passed to `new byte[len][]` / `new byte[len][]` can trigger an `OutOfMemoryError`, which is an `Error`, not an `Exception`.

### Impact Explanation
If the surrounding TVM opcode-execution dispatcher only catches `Exception` (not `Throwable`/`Error`) around precompiled-contract invocation, an `OutOfMemoryError` triggered this way can escape normal transaction-revert handling and destabilize the executing node thread/process, i.e., a denial-of-service analogous to the "crash" impact of the original CVE. Even if only `ArrayIndexOutOfBoundsException` results, this is an uncontrolled, attacker-chosen crash path inside contract execution reachable by any address able to invoke the precompile from a smart contract, which is explicitly in scope ("TVM opcodes, precompiles and energy metering").

Note: I could not fully trace, within the available search iterations, the exact call sites inside the `BatchValidateSign`/`ValidateMultiSign` inner classes that invoke `extractBytesArray`/`extractBytes32Array`, nor confirm definitively whether the outer TVM dispatcher catches `Throwable` broadly enough to downgrade this to a simple revert. This uncertainty should be resolved by tracing `Program`'s precompiled-contract invocation try/catch scope before treating this as fully confirmed high-impact.

### Likelihood Explanation
`BatchValidateSign` and `ValidateMultiSign` are gated behind `VMConfig.allowTvmSolidity059()`, a long-standing hard-fork feature flag that is enabled on TRON mainnet, so the precompiles are reachable today by any contract deployer/caller who crafts call data with an oversized length word at the expected array-length slot, requiring only a standard `TriggerSmartContract` transaction. [3](#0-2) 

### Recommendation
Bound-check `len` against `words.length - offset - 1` (and any derived `bytesOffset`/`bytesLen`) before allocating arrays or indexing `words[]` in both `extractBytesArray` and `extractBytes32Array`, mirroring the defensive checks already present in `ContractEventParser.subBytes` (`Kohvert/java-tron--021` `framework/src/main/java/org/tron/common/logsfilter/ContractEventParser.java:80-91`), which explicitly rejects oversized/negative lengths instead of letting them reach array allocation or `System.arraycopy`.

### Proof of Concept
1. Deploy or call a contract that performs a `STATICCALL`/`CALL` to precompiled address `0x9` (`BatchValidateSign`) or `0xa` (`ValidateMultiSign`) with call data crafted so the ABI-decoded array-length word at the expected offset is a very large positive value (e.g., `0x7fffffff`) while the remainder of the call data is short.
2. During `extractBytesArray`/`extractBytes32Array` decoding, the loop attempts to read `words[offset + i + 1]` far past the actual `words` array length, throwing `ArrayIndexOutOfBoundsException`, or the initial `new byte[len][]` allocation attempts a multi-gigabyte allocation, throwing `OutOfMemoryError`.
3. Observe whether the exception/error propagates past the expected "revert the transaction" handling in the TVM interpreter, and whether it destabilizes the executing node process (this final confirmation step requires runtime testing not available via static code search).

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
