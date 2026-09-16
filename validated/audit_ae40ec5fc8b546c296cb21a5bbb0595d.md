This is a governable hard-fork flag (`ALLOW_TVM_SELFDESTRUCT_RESTRICTION`, controlled via `ProposalUtil`/`DynamicPropertiesStore`), so whether the unbounded `extractBytesArray` path in `ValidateMultiSign`/`BatchValidateSign` is reachable on a given network depends on whether that proposal has been activated. I could not confirm from the index whether it is currently active on java-tron mainnet, so I can't fully assert current-network reachability.

Based on everything gathered, here is my assessment:

### Title
Unbounded attacker-controlled array length in `ValidateMultiSign`/`BatchValidateSign` precompiles can trigger `OutOfMemoryError`/crash before size validation - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
`PrecompiledContracts.extractBytesArray()` reads a signature-array length directly from attacker-supplied TVM call data and immediately allocates a `byte[len][]` array with that length, before any upper-bound check is applied.

### Finding Description
`extractBytesArray` computes `len = words[offset].intValueSafe()` from caller-controlled `data` and immediately does `new byte[len][]`: [1](#0-0) 

This is used by `ValidateMultiSign.execute()` and `BatchValidateSign.doExecute()` to build the `signatures` array. The `MAX_SIZE` bound (5 or 16) is only enforced *after* the array has already been allocated, and only when `VMConfig.allowTvmSelfdestructRestriction()` is true — the legacy branch calls `extractBytesArray` unconditionally with no pre-check: [2](#0-1) [3](#0-2) 

This mirrors the CVE-2025-64096 bug class: a length field taken from attacker-controlled input is used directly to size an array without validating it against any safe bound first, allowing the attacker to force an oversized allocation. In Java this manifests as `OutOfMemoryError`/`NegativeArraySizeException` rather than classic stack memory corruption, but the root cause (attacker length field → unchecked allocation) is structurally the same.

### Impact Explanation
The `extractBytesArray` call in `ValidateMultiSign.execute()` sits *outside* the method's inner `try/catch(Throwable)` block that wraps only the signature-recovery logic, so an `Error` thrown during array allocation would propagate out of `execute()` uncaught by that local handler. Whether this ultimately crashes/halts the node depends on exception handling further up the TVM call stack, which I was not able to fully trace within the scope of this review.

### Likelihood Explanation
Exploitability is conditioned on the `ALLOW_TVM_SELFDESTRUCT_RESTRICTION` hard-fork flag being inactive for the legacy (unguarded) branch to be reachable; I could not confirm the current activation status of this proposal on java-tron mainnet from the indexed code, so likelihood is uncertain without that confirmation.

### Recommendation
Move the `MAX_SIZE` (or equivalent) bound check on `len`/`sigArraySize` inside `extractBytesArray`/`extractSigArray` themselves, before allocating `byte[len][]`, so the check applies unconditionally regardless of `allowTvmSelfdestructRestriction()`.

### Proof of Concept
A contract calling the `ValidateMultiSign` precompile with an ABI-encoded blob where the signature-count word (`words[3]`-relative offset) is set to a very large positive integer (e.g., close to `Integer.MAX_VALUE`) would, on a network where `allowTvmSelfdestructRestriction()` is false, cause `extractBytesArray` to attempt `new byte[len][]` before any bound check runs.

**Caveat:** Because I could not verify (a) the current activation status of `ALLOW_TVM_SELFDESTRUCT_RESTRICTION` on mainnet, and (b) whether an `Error` thrown here is actually caught/contained by an outer TVM/`Program` handler (which would reduce this to a per-call revert rather than a node crash), I present this with lower confidence than a fully validated finding. If you want a definitive verdict, a Devin session with full repo/tooling access could trace the exception-handling path from `PrecompiledContracts.execute()` up through `Program`/`VM` to confirm whether an `Error` here actually terminates block processing versus being caught and converted into a reverted call.

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1057-1078)
```java
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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1162-1181)
```java
      DataWord[] words = DataWord.parseArray(data);
      byte[] hash = words[0].getData();

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
      byte[][] addresses = extractBytes32Array(
          words, words[2].intValueSafe() / WORD_SIZE);
      int cnt = signatures.length;
      if (cnt == 0 || cnt > MAX_SIZE || signatures.length != addresses.length) {
        return Pair.of(true, DATA_FALSE);
      }
```
