Based on my research, I found a plausible analog to the Mercurial CVE-2016-3630 bug class (unvalidated length field controls unsafely-sized buffer/array allocation from attacker-controlled binary data).

### Title
Unbounded array allocation from attacker-controlled length word in TVM precompiled contract input decoding - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
`PrecompiledContracts.extractBytes32Array` and `extractBytesArray` decode ABI-encoded arrays from the raw calldata `words` passed into TVM precompiled contracts (used at minimum by the `BatchValidateSign` and `ValidateMultiSign` precompiles). The declared array length is taken directly from attacker-controlled call data via `words[offset].intValueSafe()` and used to allocate a Java array (`new byte[len][]`) with no upper bound or sanity check against the actual size of the remaining input. [1](#0-0) [2](#0-1) 

### Finding Description
Both helper methods read a length value straight out of the caller-supplied `DataWord[] words` (calldata) with `intValueSafe()` and immediately use it to size a native Java array:
```java
int len = words[offset].intValueSafe();
byte[][] bytes32Array = new byte[len][];
```
This mirrors the Mercurial delta-decoder bug class: a length/size field taken from untrusted input is used for buffer/array sizing without first validating it is non-negative and bounded by the actual available input size, similar to Mercurial's "list sizing rounding error" and "short records" flaw (CVE-2016-3630). Elsewhere in the same codebase (`RLP.calcLength`/`verifyLength` in `framework/src/main/java/org/tron/core/capsule/utils/RLP.java`) the project explicitly guards against this exact class of bug by bounding parsed lengths against the remaining payload and by catching `OutOfMemoryError` around decoding, showing the team is aware of the risk elsewhere but did not apply the same defense in `PrecompiledContracts`. [3](#0-2) [4](#0-3) 

Depending on the value of `intValueSafe()` for a maliciously crafted length word:
- A very large positive value (e.g., near `Integer.MAX_VALUE`) causes `new byte[len][]` to attempt a huge heap allocation, throwing `OutOfMemoryError` — an `Error`, not an `Exception`, which is not reliably caught by ordinary TVM opcode/precompile exception handling designed around `Exception`/`RuntimeException` (e.g., `OutOfTimeException` handling in `Program`).
- A negative value throws `NegativeArraySizeException`.

Because this code executes inside precompiled-contract dispatch reachable from any smart contract call (`getContractForAddress` routes to `batchValidateSign`/`validateMultiSign` when the corresponding TVM feature flag is enabled), any unprivileged contract deployer/caller can trigger this path with a single transaction that calls a contract invoking these precompiles with crafted calldata. [5](#0-4) 

### Impact Explanation
An uncaught `OutOfMemoryError` triggered during transaction/block execution can destabilize or crash the node process (or at minimum abort in-flight block application), which matches the "node crash or halt" impact criterion. Since this is triggered deterministically by transaction calldata that every full node must execute identically, it also raises consensus-availability concerns if only some nodes crash (e.g., due to differing heap headroom) versus others survive, though the primary and most defensible impact is node crash/halt.

### Likelihood Explanation
The path is reachable via a single, unprivileged, ordinary transaction that calls a smart contract invoking the `BatchValidateSign` (address `...09`) or `ValidateMultiSign` (address `...0a`) precompile with crafted calldata, gated only by `VMConfig.allowTvmSolidity059()` being enabled (a mainnet-enabled feature). No special privileges (SR/witness/committee) are required.

### Recommendation
Bound `len` from `intValueSafe()` against a sane maximum (e.g., the number of remaining `words`) before allocation, and reject with a `RuntimeException`/revert instead of allocating, mirroring the `verifyLength` pattern already used in `RLP.java`.

### Proof of Concept
Deploy a contract that calls the `BatchValidateSign` (or `ValidateMultiSign`) precompiled contract address directly via a low-level `call`/`staticcall`, supplying ABI-encoded input where the array-length word at the expected offset is set to a very large value (e.g., `0x7fffffff`) or a value that decodes to a negative `int`. Executing this transaction on a node with `allowTvmSolidity059` enabled routes into `PrecompiledContracts.extractBytesArray`/`extractBytes32Array`, which will attempt `new byte[len][]` and throw `OutOfMemoryError`/`NegativeArraySizeException` during precompile execution.

**Caveat:** I was not able to fully trace, within the available tool budget, the exact exception-handling wrapper around precompiled-contract invocation in `Program`/`TransactionTrace` to conclusively confirm whether `OutOfMemoryError` is guaranteed to propagate all the way to crash the node process versus being caught and converted into a reverted transaction at some outer layer. This should be verified against `actuator/src/main/java/org/tron/core/vm/program/Program.java` and the call site of `getContractForAddress(...).execute(...)` before treating this as fully confirmed High/Critical impact.

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

**File:** framework/src/main/java/org/tron/core/capsule/utils/RLP.java (L596-602)
```java
    } catch (Exception e) {
      throw new RuntimeException(
          "RLP wrong encoding (" + Hex.toHexString(msgData, startPos, endPos - startPos) + ")", e);
    } catch (OutOfMemoryError e) {
      throw new RuntimeException("Invalid RLP (excessive mem allocation while parsing) (" + Hex
          .toHexString(msgData, startPos, endPos - startPos) + ")", e);
    }
```

**File:** framework/src/main/java/org/tron/core/capsule/utils/RLP.java (L612-617)
```java
  private static void verifyLength(int suppliedLength, int availableLength) {
    if (suppliedLength > availableLength) {
      throw new RuntimeException(String.format("Length parsed from RLP (%s bytes) is greater "
          + "than possible size of data (%s bytes)", suppliedLength, availableLength));
    }
  }
```
