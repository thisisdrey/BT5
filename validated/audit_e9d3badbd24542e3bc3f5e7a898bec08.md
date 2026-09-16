### Title
Unbounded attacker-controlled length in `ValidateMultiSign` precompile allocation causes uncaught OOM / node instability - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The GLib CVE root cause is an integer-overflow-driven heap allocation on an attacker-supplied length field inside `escape_byte_string()`. The closest reachable analog in java-tron is in the `ValidateMultiSign` precompiled contract (address `0x...0a`), where a length value taken directly from attacker-controlled call data is used to size a Java array without any bound check, and the call site is **not** wrapped by the defensive `catch (Throwable)` that protects the sibling `BatchValidateSign` precompile.

### Finding Description
`ValidateMultiSign.execute()` decodes the raw call data into `DataWord[] words` and then computes the signature array length straight from attacker data: [1](#0-0) 

The length used to build the `byte[][]` array (`extractBytesArray`/`extractSigArray`) comes from `words[offset].intValueSafe()`: [2](#0-1) 

The `MAX_SIZE` bound (`sigArraySize > MAX_SIZE`) is only enforced **when `VMConfig.allowTvmSelfdestructRestriction()` is active**: [3](#0-2) 

When that feature flag is not enabled (or on chains where it has not been activated), `extractBytesArray` is invoked directly with an attacker-chosen `len`, allocating `new byte[len][]` with no upper bound, before any `try/catch` boundary is reached (the only `try/catch` in this method wraps the later `account != null` weight-computation block, not the array extraction).

By contrast, the sibling precompile `BatchValidateSign` wraps its entire `doExecute` call in a `catch (Throwable t)`: [4](#0-3) 

`ValidateMultiSign` has no equivalent outer guard, so an `OutOfMemoryError` (or `NegativeArraySizeException`/`ArrayIndexOutOfBoundsException` from a crafted offset) thrown while sizing `bytes32Array`/`bytesArray` propagates out of the precompile call uncaught within this actuator path.

### Impact Explanation
An `OutOfMemoryError` triggered inside a JVM validating node is not confined to a single transaction — it can destabilize the whole node process (GC thrash, other threads failing allocations, potential crash), which is a denial-of-service on that node analogous to the GLib DoS/crash triggered by the integer-overflow-driven allocation. Because block application must be deterministic across all full nodes, a transaction that reliably triggers this on one node will trigger it on every node that executes the block, giving an attacker a low-cost vector to knock validating nodes offline network-wide.

### Likelihood Explanation
This precompile is reachable by any account: a contract can be deployed and invoked (or an existing malicious contract can be crafted) to `CALL` the `validateMultiSign` precompiled address with crafted call data specifying an oversized "array length" word. No special privileges, SR/witness status, or non-standard node configuration is required beyond the precompile being active on the chain (activated via `allowTvmSolidity059`), which is standard on running networks. The `MAX_SIZE` bound only applies under a separate, distinct feature flag (`allowTvmSelfdestructRestriction`), so this is exploitable on any deployment where that specific flag has not (yet) been activated.

### Recommendation
- Enforce the `sigArraySize > MAX_SIZE` bound (and equivalent bound on `extractBytesArray`/`extractBytes32Array` lengths) unconditionally, not gated behind `allowTvmSelfdestructRestriction()`.
- Validate that `len` from `intValueSafe()` is non-negative and within a small sane ceiling before allocating any array in `extractBytes32Array`/`extractBytesArray`/`extractSigArray`.
- Wrap `ValidateMultiSign.execute()` in a top-level `catch (Throwable)` mirroring `BatchValidateSign.execute()`, returning `DATA_FALSE`/failure instead of letting an uncaught error escape the actuator.
- Adopt the same `isValidAbiEncoding` shape-validation gate used for Osaka activation as an always-on check for both precompiles, independent of feature-flag activation state.

### Proof of Concept
1. Deploy a minimal contract that performs `CALL` to precompile address `0x0…0a` (`validateMultiSign`).
2. Encode call data per `validatemultisign(address,uint256,bytes32,bytes[])` such that the dynamic `bytes[]` array's length word (at the offset referenced by `words[3]`) is set to a very large value (e.g. `0x7fffffff`), while keeping the actual payload short.
3. On a chain/node where `allowTvmSelfdestructRestriction` has not been activated, invoke this contract via a normal signed transaction.
4. `extractBytesArray` executes `new byte[len][]` with `len = 0x7fffffff`, immediately throwing `OutOfMemoryError` outside any protective `catch`, propagating out of the precompile call and destabilizing the executing node's JVM.

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1057-1074)
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
