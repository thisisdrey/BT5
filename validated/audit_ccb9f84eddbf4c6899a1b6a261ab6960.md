### Title
Unbounded array allocation in `ValidateMultiSign` precompile allows attacker-controlled OOM / node crash - ([File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java])

### Summary
The `ValidateMultiSign` TVM precompiled contract accepts a length field taken directly from attacker-controlled call data and uses it to allocate a Java array *before* validating it against any sane bound, mirroring the managesieve CVE-2026-27858 pattern of a pre-validation message field driving a large allocation.

### Finding Description
`ValidateMultiSign.execute` extracts a signature-array length from the ABI-encoded call data and, in the legacy (non-`allowTvmSelfdestructRestriction`) code path, passes it straight to `extractBytesArray` without any prior bound check: [1](#0-0) 

`extractBytesArray` allocates `new byte[len][]` using that unchecked `len` value taken from `words[offset].intValueSafe()`: [2](#0-1) 

The size guard `sigArraySize > MAX_SIZE` only exists inside the `allowTvmSelfdestructRestriction()` branch, gating the *newer* `extractSigArray` call, but the older `extractBytesArray` branch is reached with no such check: [1](#0-0) 

Even the post-hoc bound check `signatures.length == 0 || signatures.length > MAX_SIZE` only runs *after* `extractBytesArray`/`extractSigArray` has already returned — i.e., after the oversized allocation attempt has already happened: [3](#0-2) 

Energy cost for this precompile (`getEnergyForData`) is computed purely from `data.length / WORD_SIZE`, i.e. from the *calldata size*, not from the embedded `len` field: [4](#0-3) 

This means a caller can submit a small, cheap calldata blob (few words, low energy cost) that encodes a single word with a huge integer value (up to `Integer.MAX_VALUE`, subject to `intValueSafe()`'s clamping) at the position read as `len`, forcing `new byte[len][]` to attempt to allocate an array of hundreds of millions of object references — potentially gigabytes of heap — for negligible energy expenditure.

### Impact Explanation
Any unprivileged account can deploy or call a smart contract that invokes the `ValidateMultiSign` precompile address with crafted calldata. Because the huge allocation happens before the size sanity check and is not proportional to the energy paid, a single cheap transaction can trigger a multi-hundred-MB/GB allocation attempt on every full node and SR node that executes the transaction (including during block validation/re-execution by all nodes in the network), risking `OutOfMemoryError`, GC thrashing, and node crash/halt — a network-wide denial of service analogous to the managesieve pre-auth allocation DoS in the reported CVE.

### Likelihood Explanation
High. No special privilege, signature authority, or SR/witness status is required — only the ability to broadcast a transaction that calls a contract invoking the precompile at the fixed `ValidateMultiSign` address, which is reachable by any TVM contract deployer/caller. The trigger is a single crafted word in calldata; no race condition or timing is needed.

### Recommendation
Move the `sigArraySize`/`len` bound check (`> MAX_SIZE`) to before array allocation in both `extractBytesArray` and `extractSigArray`, and apply it unconditionally (not only under the `allowTvmSelfdestructRestriction` feature flag) so that the legacy code path is protected identically to the newer path.

### Proof of Concept
Construct ABI-encoded calldata for `ValidateMultiSign` where `words[3]` points to an offset whose word value (`len`) is set to a very large integer (e.g., `0x7FFFFFFF` or another value that survives `intValueSafe()`), while keeping the overall calldata short (few 32-byte words) to minimize energy cost. Deploy a trivial contract that calls this precompile address with that calldata and broadcast the transaction on a network where `allowTvmSelfdestructRestriction` is not yet activated (or confirm the same unguarded pattern exists pre-activation across historical chain states). On execution, `extractBytesArray` executes `new byte[len][]`, attempting a huge allocation, before the `signatures.length > MAX_SIZE` check ever runs.

Note: I could not fully verify the exact upper bound enforced by `DataWord.intValueSafe()` (whether it clamps to `Integer.MAX_VALUE` or some other cap) within available exploration, nor whether `allowTvmSelfdestructRestriction` is already activated on the live mainnet (which would mean only historical/legacy-configured networks are exposed to the unguarded `extractBytesArray` path). This should be confirmed in a live/full-source review.

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1044-1049)
```java
    @Override
    public long getEnergyForData(byte[] data) {
      long cnt = (data.length / WORD_SIZE - 5) / 5;
      // one sign 1500, half of ecrecover
      return cnt * ENGERYPERSIGN;
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1066-1078)
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

      if (signatures.length == 0 || signatures.length > MAX_SIZE) {
        return Pair.of(true, DATA_FALSE);
      }
```
