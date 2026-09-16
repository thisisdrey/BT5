### Title
Unbounded array allocation from unvalidated length field in precompiled contract signature-array decoding - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
`extractBytesArray` and `extractSigArray` in `PrecompiledContracts.java` allocate a Java array whose size is taken directly from an attacker-controlled 32-byte calldata word, without verifying that the claimed element count is consistent with the amount of calldata actually supplied. This mirrors the rpcbomb (CVE-2017-8779) root cause: a length field is trusted for memory allocation before the corresponding payload is validated, allowing a small request to trigger a disproportionately large allocation.

### Finding Description
`extractBytesArray` and `extractSigArray` read a "length" word (`len = words[offset].intValueSafe()`) from the ABI-encoded input to a precompiled contract and immediately allocate `new byte[len][]`: [1](#0-0) [2](#0-1) 

The only bounds check performed is on `offset` itself (`offset > words.length - 1`), not on `len`. `len` comes straight from `intValueSafe()` on a `DataWord`, which can be an attacker-chosen large positive integer (up to `Integer.MAX_VALUE`), completely decoupled from how many actual words follow in `data`. The subsequent `for` loop that populates the array only fails (with an index/array-bounds exception) *after* the array of `len` null-reference slots has already been allocated. `extractBytes32Array` has the same pattern: [3](#0-2) 

These helpers are used by the multi-signature validation precompiled contracts (`ValidateMultiSign`/`BatchValidateSign`), which are reachable from any TVM contract call — i.e., from an unprivileged, unpermissioned transaction that simply invokes the precompile address with crafted calldata. A crafted call can set the length word to a very large value while keeping the physical calldata short, causing the node to attempt an outsized array allocation before any content-consistency check occurs, analogous to how rpcbind allocated memory for an XDR string based on the claimed length field without checking it against the actual received data.

### Impact Explanation
A successful trigger allocates a large object array (`byte[][]`) sized by an attacker-chosen value with no upper bound tied to the real payload size. Repeated or single large invocations can induce significant, uncontrolled heap pressure/GC churn or an `OutOfMemoryError`, which can crash or stall the node process handling the transaction — a denial-of-service condition consistent with the "node crash or halt" impact criteria.

### Likelihood Explanation
The path is reachable by any account able to broadcast a transaction that calls a contract exercising these precompiles (multi-signature verification precompiled addresses), requiring no special privilege, stake, or node cooperation — only a single crafted transaction/contract call. The severity of the DoS is bounded in practice by whatever energy limit is charged for the corresponding precompile call; whether the precompile's `getEnergyForData` cost model scales strictly with the declared array length (and is charged before `execute()` runs) is not something I could conclusively verify with the available context, so the actual exploitable magnitude of the allocation (i.e., how large `len` can practically be made before energy exhaustion aborts the call) is uncertain.

### Recommendation
In `extractBytesArray`, `extractSigArray`, and `extractBytes32Array`, validate `len` against the amount of remaining calldata (e.g., require `len <= (words.length - offset - 1)` or an equivalent bound derived from `data.length`) before allocating the array, rejecting the call early if the claimed length exceeds what the actual payload can support — mirroring the fix pattern used elsewhere in the codebase, such as `ContractEventParser.subBytes`, which explicitly rejects oversized lengths instead of allocating first: [4](#0-3) 

### Proof of Concept
1. Deploy or call an existing contract that invokes the `ValidateMultiSign`/`BatchValidateSign` precompiled contract address with attacker-controlled calldata.
2. Craft the calldata so the array-length word read by `extractBytesArray`/`extractSigArray` (at the byte offset corresponding to the signatures/messages array) encodes a very large positive integer (e.g., close to `Integer.MAX_VALUE`), while keeping the actual physical calldata short.
3. Submit the transaction; `PrecompiledContracts.execute()` invokes the vulnerable helper, which allocates `new byte[len][]` sized by the attacker-controlled value before any content bound check occurs, consuming excessive heap memory on the executing node.

Note: I was unable to fully verify, within the available tool budget, the exact energy-cost gating (`getEnergyForData`) for these specific precompiles to confirm precisely how large `len` can be made before energy exhaustion intervenes; this should be confirmed by inspecting `ValidateMultiSign`/`BatchValidateSign`'s `getEnergyForData` implementation and its relationship to `data.length` versus the internal `len` field.

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

**File:** framework/src/main/java/org/tron/common/logsfilter/ContractEventParser.java (L80-87)
```java
  protected static byte[] subBytes(byte[] src, int start, int length) {
    if (ArrayUtils.isEmpty(src)) {
      throw new OutputLengthException("source data is empty");
    }
    if (start < 0 || start >= src.length || length < 0 || length > src.length - start) {
      throw new OutputLengthException(
          "data start:" + start + ", length:" + length + ", src.length:" + src.length);
    }
```
