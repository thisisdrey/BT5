### Title
Unbounded array allocation from attacker-controlled length in `ValidateMultiSign` precompile enables OOM-based node crash - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The `ValidateMultiSign` TVM precompiled contract parses attacker-supplied calldata into `byte[][]` arrays whose size is taken directly, and without an upper bound in the default code path, from a caller-controlled ABI word before any signature-count validation occurs. A single crafted contract call can force the executing node to allocate a reference array sized by an attacker-chosen 32-byte word, causing an `OutOfMemoryError` during TVM execution, analogous to CVE-2022-2529 (GoFlow sflow decoder) where insufficient length sanitization before allocation allowed memory-exhaustion DoS.

### Finding Description
`ValidateMultiSign.execute()` reads the signature-array length from calldata and dispatches to one of two extraction helpers: [1](#0-0) 

The length-guard (`sigArraySize > MAX_SIZE`) that bounds the array *before* allocation is only executed `if (VMConfig.allowTvmSelfdestructRestriction())`. When that fork flag is not active, `extractBytesArray` is invoked directly, whose length comes straight from `words[offset].intValueSafe()` with no upper bound check prior to allocating the array: [2](#0-1) 

`extractSigArray`, used on the "restricted" branch, still allocates `byte[len][]` from the same unguarded `len` before the caller applies its own `sigArraySize > MAX_SIZE` check — but critically, on the pre-fork/unpatched path the `MAX_SIZE` pre-check at line 1066-1071 is skipped entirely, so `extractBytesArray`'s `new byte[len][]` executes with a fully attacker-controlled `len` (up to whatever `intValueSafe()` yields, effectively up to `Integer.MAX_VALUE`).

This mirrors the GoFlow sflow flaw exactly: a length field taken from untrusted, attacker-supplied bytes is used to drive a large allocation before any sanity/size validation is performed, letting a single malicious input exhaust memory.

### Impact Explanation
Because TVM precompiles execute deterministically on every full node that validates the transaction/block (not just the sender), a single transaction invoking `ValidateMultiSign` with a crafted `sigArray` length word can trigger a massive `byte[len][]` allocation across the entire validating network simultaneously. This can throw `OutOfMemoryError`, destabilizing or crashing node JVMs, resulting in denial of service / potential chain halt — matching the "node crash or halt" acceptance criterion.

### Likelihood Explanation
Reachability requires only an unprivileged, unpermissioned smart-contract call (`TriggerSmartContract`) to the `ValidateMultiSign` precompiled-contract address with crafted calldata — no special account permission, stake, or witness/SR privilege is needed. Exploitability is contingent on `VMConfig.allowTvmSelfdestructRestriction()` being false (the pre-hardening state of this code path); it is unclear from the available code whether this flag is currently activated in the maintained mainnet configuration, so likelihood should be treated as network/state dependent rather than confirmed exploitable in the current live chain configuration.

### Recommendation
Validate the extracted `len` against `MAX_SIZE` (or another explicit sane bound) *before* calling `extractBytesArray`/`extractSigArray`/`extractBytes32Array`, unconditionally — not gated behind a hard-fork flag — so the length check always executes prior to any array allocation, for both the legacy and restricted code paths.

### Proof of Concept
1. Deploy or use any contract capable of issuing a `staticcall`/`call` to the `ValidateMultiSign` precompile address with raw ABI-encoded calldata.
2. Construct calldata such that: `words[3]` (the offset word) points to a location where the "array length" word (`words[offset]`) is set to an extremely large value, e.g. `0x7FFFFFFF`.
3. Ensure `VMConfig.allowTvmSelfdestructRestriction()` is not yet enabled (legacy path), so execution falls to `extractBytesArray(words, offset, rawData)` at: [3](#0-2) 
4. `extractBytesArray` executes `new byte[len][]` with `len = 0x7FFFFFFF`, attempting to allocate a huge reference array before the subsequent `signatures.length > MAX_SIZE` check ever runs, causing `OutOfMemoryError` on every node processing the transaction.

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
