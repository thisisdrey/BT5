### Title
Integer/length overflow in `ValidateMultiSign` precompile leading to unbounded memory allocation before size validation - ([File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java])

### Summary
The `ValidateMultiSign` precompiled contract (address `0x...09`, callable from any TVM contract via a normal `TriggerSmartContract`) decodes an attacker-controlled 32-byte "array length" word from the raw call data and immediately allocates a `byte[len][]` array with that length, before any upper-bound check is applied on the legacy execution path. This mirrors the reported virtio-snd class of bug: a guest/caller-controlled count field drives host/node memory allocation with insufficient validation, enabling denial of service.

### Finding Description
`extractBytesArray` and `extractSigArray` read the array length directly from the caller-supplied word array and allocate accordingly: [1](#0-0) [2](#0-1) 

`len` comes from `words[offset].intValueSafe()`, which for any value that doesn't fit safely into an `int` is clamped to `Integer.MAX_VALUE` rather than rejected: [3](#0-2) 

In `ValidateMultiSign.execute`, the length-bounding check (`sigArraySize > MAX_SIZE`) is only performed when `VMConfig.allowTvmSelfdestructRestriction()` is enabled, and even then it is a *separate* re-read of the same length field before calling `extractSigArray`. On the legacy code path (`allowTvmSelfdestructRestriction()` disabled), `extractBytesArray` is invoked directly with the raw, unvalidated length, and the `signatures.length > MAX_SIZE` check only happens *after* the array has already been allocated: [4](#0-3) 

Because `intValueSafe()` allows the decoded length to be as large as `Integer.MAX_VALUE` regardless of the actual size of the submitted call data, a caller can supply a tiny calldata blob (a few 32-byte words) that merely *claims* an enormous array length, causing `new byte[Integer.MAX_VALUE][]` (or `new byte[len][192]` in `extractSigArray`) to be attempted. This is the same bug class as the CVE: a count/length field from an untrusted source is trusted for sizing an allocation without being cross-checked against the actual available/declared payload size, and without an upper bound being enforced prior to allocation.

### Impact Explanation
An attempted allocation of this magnitude throws `OutOfMemoryError`. Because Java's `OutOfMemoryError` is a JVM-wide condition (shared heap), it can destabilize or crash the entire java-tron node process handling the transaction (full node, SR node, or any node executing/re-executing the block), not just the single execution thread. This satisfies the "node crash or halt" impact criterion — an unprivileged, remotely-submitted transaction can induce a denial-of-service condition on any node that must execute it (including during block validation by all consensus participants), which can affect chain liveness if it hits multiple SRs simultaneously.

### Likelihood Explanation
The precompile is reached by any account able to broadcast a `TriggerSmartContract` transaction that calls address `0x...09` — no special privilege, deployment, or state setup is required beyond an existing contract call. Energy cost of the call (`getEnergyForData`) is computed from `data.length` (the actual submitted call-data size), which can be kept small, while the internally-decoded "length" word can independently claim a value near `Integer.MAX_VALUE`, so the attacker pays for a small transaction but triggers an oversized allocation attempt. The main uncertainty is whether the target chain's `allowTvmSelfdestructRestriction` hard-fork flag is enabled; if enabled, `extractSigArray`'s length is checked immediately before allocation, closing this particular path — but the legacy/pre-fork code (and `extractBytesArray`, still used elsewhere for the non-restricted case) has no such precondition. This is likelihood-moderate, contingent on fork-flag state, but present as unguarded code in the current codebase.

### Recommendation
- Validate the decoded array-length field (`len` in `extractBytesArray`/`extractSigArray`/`extractBytes32Array`) against both the `MAX_SIZE` bound and the actual remaining size of `data`/`rawData` *before* allocating any array, independent of the `allowTvmSelfdestructRestriction` flag.
- Reject the call (return `false`/`DATA_FALSE`) if the declared length would require reading beyond the bounds of the supplied call data, rather than relying on `intValueSafe()`'s silent clamp to `Integer.MAX_VALUE`.
- Apply the same pre-allocation bound check uniformly across all callers of `extractBytesArray`, `extractSigArray`, and `extractBytes32Array` in `PrecompiledContracts.java`.

### Proof of Concept
1. Craft a `TriggerSmartContract` transaction whose contract calls the `ValidateMultiSign` precompile (`0x0000000000000000000000000000000000000009`) with ABI-encoded input where:
   - `words[3]` (the signatures-array pointer) points to an offset within the small provided calldata.
   - The word at that offset (the declared `len` for the signature/bytes array) is set to `0x7fffffff` (`Integer.MAX_VALUE`).
2. Ensure `VMConfig.allowTvmSelfdestructRestriction()` is not active for the targeted chain/fork state (legacy path), so execution reaches: [5](#0-4) 
   calling `extractBytesArray(words, offset, rawData)` directly with `len = 0x7fffffff`.
3. Inside `extractBytesArray`, `new byte[len][]` at line 404 attempts to allocate a ~17GB array of references, throwing `OutOfMemoryError` and destabilizing/crashing the node's JVM before the post-hoc `signatures.length > MAX_SIZE` check at line 1076 is ever reached.

### Citations

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1052-1078)
```java
    public Pair<Boolean, byte[]> execute(byte[] rawData) {
      if (VMConfig.allowTvmOsaka()
          && !isValidAbiEncoding(rawData, ABI_HEADER_WORDS, ABI_ITEM_WORDS)) {
        return Pair.of(false, EMPTY_BYTE_ARRAY);
      }
      DataWord[] words = DataWord.parseArray(rawData);
      byte[] address = words[0].toTronAddress();
      int permissionId = words[1].intValueSafe();
      byte[] data = words[2].getData();

      byte[] combine = ByteUtil.merge(address, ByteArray.fromInt(permissionId), data);
      byte[] hash = Sha256Hash.hash(CommonParameter
          .getInstance().isECKeyCryptoEngine(), combine);

      if (VMConfig.allowTvmSelfdestructRestriction()) {
        int sigArraySize = words[words[3].intValueSafe() / WORD_SIZE].intValueSafe();
        if (sigArraySize > MAX_SIZE) {
          return Pair.of(true, DATA_FALSE);
        }
      }
      byte[][] signatures = VMConfig.allowTvmSelfdestructRestriction() ?
          extractSigArray(words, words[3].intValueSafe() / WORD_SIZE, rawData) :
          extractBytesArray(words, words[3].intValueSafe() / WORD_SIZE, rawData);

      if (signatures.length == 0 || signatures.length > MAX_SIZE) {
        return Pair.of(true, DATA_FALSE);
      }
```

**File:** framework/src/test/java/org/tron/common/utils/client/utils/DataWord.java (L130-140)
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
