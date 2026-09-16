Based on my investigation, I found a plausible analog reachable from a signed transaction.

### Title
Unbounded array allocation in `ValidateMultiSign` precompile signature/address extraction enables node OutOfMemoryError - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
The `ValidateMultiSign` precompiled contract (address `0x...a`) parses a contract-call's `bytes[]` signature array by reading an attacker-controlled length word directly from calldata and immediately allocating a Java array of that size, with no upper bound unless a specific feature switch is active. This mirrors the libtiff CVE-2020-35521 root cause: an untrusted length field from input data is used to drive a memory allocation without validation, leading to an out-of-memory abort.

### Finding Description
`ValidateMultiSign.execute` calls `extractBytesArray`/`extractSigArray` on attacker-supplied calldata: [1](#0-0) 

The size check (`sigArraySize > MAX_SIZE`) is only performed when `VMConfig.allowTvmSelfdestructRestriction()` is enabled: [2](#0-1) 

If that feature switch is not active, execution falls straight into `extractBytesArray`, which reads the length word from calldata and allocates a two-dimensional array sized directly by that untrusted value with no bound check: [3](#0-2) 

Because `words[offset].intValueSafe()` can return a value up to `Integer.MAX_VALUE`, `new byte[len][]` can trigger an immediate `OutOfMemoryError`. This allocation, and the subsequent per-item allocation of `bytesArray[i]` inside the loop, occurs outside of any `try/catch` in `ValidateMultiSign.execute` — unlike `BatchValidateSign.execute`, which wraps its equivalent logic in a `try { doExecute(data) } catch (Throwable t)` guard: [4](#0-3) 

`ValidateMultiSign` has no such wrapper, so an `OutOfMemoryError`/`Error` thrown during extraction propagates unhandled out of the precompile invocation and up through the TVM `Program` execution path.

### Impact Explanation
An `Error` (not `Exception`) escaping the TVM interpreter during precompile execution is not the same as a normal revert; depending on how the encompassing `Manager`/`TransactionTrace` block-application code handles `Throwable` vs `Exception`, an uncaught `OutOfMemoryError` can destabilize or crash the node process handling the block, which is a denial-of-service against the ability of the node to keep serving/validating transactions — directly analogous to the "resulting in denial of service" impact in CVE-2020-35521.

### Likelihood Explanation
This finding is **conditional and not fully proven** from static reading alone:
- Exploitability requires `VMConfig.allowTvmSelfdestructRestriction()` to be **disabled**. I was unable to confirm from the index whether this hard-fork feature switch is enabled by default in the current mainnet chain parameters (`DynamicPropertiesStore` references it, but I could not inspect its activation state in this session).
- I was also unable to verify, due to iteration limits, whether the caller of precompiled contracts (`Program`/`VM` classes) wraps `execute()` calls in a `catch (Throwable)`/`catch (Error)` block that would downgrade an `OutOfMemoryError` to a contract revert rather than crashing the node.

Given these two open questions, I cannot assert with confidence that this is currently exploitable in production — if the restriction flag is already activated network-wide (likely, since this is an older TIP) and/or the VM wraps precompile execution in a `Throwable` catch, this reduces to at most a per-transaction revert (no real impact), which would fall outside the required severity bar.

### Recommendation
Regardless of current activation state, `extractBytesArray`, `extractSigArray`, and `extractBytes32Array` should enforce the same `MAX_SIZE` bound unconditionally (not only when `allowTvmSelfdestructRestriction()` is set), and `ValidateMultiSign.execute` should wrap the parsing/extraction logic in a `try/catch` (as `BatchValidateSign` already does) to prevent any `Throwable` from escaping precompile execution.

### Proof of Concept
Not concretely demonstrated. A crafted call to the `validatemultisign(address,uint256,bytes32,bytes[])` precompile at address `0x...a` with the ABI-encoded length word for the `bytes[]` array set to a very large value (e.g., `0x7fffffff`) would, in the pre-`allowTvmSelfdestructRestriction` code path, cause `extractBytesArray`/`extractSigArray` to attempt `new byte[0x7fffffff][]`, but I could not confirm in this session that this configuration is reachable on a live/default-configured node, nor confirm the resulting error is not caught upstream — this needs to be verified with a running node/test harness before treating this as a confirmed vulnerability.

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1144-1154)
```java
    @Override
    public Pair<Boolean, byte[]> execute(byte[] data) {
      try {
        return doExecute(data);
      } catch (Throwable t) {
        if (t instanceof InterruptedException){
          Thread.currentThread().interrupt();
        }
        return Pair.of(true, new byte[WORD_SIZE]);
      }
    }
```
