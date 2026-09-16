### Title
Unbounded, attacker-controlled array length in `BatchValidateSign` precompile allocation causes memory-exhaustion DoS - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The `batchvalidatesign(bytes32,bytes[],address[])` precompiled contract (`PrecompiledContracts.BatchValidateSign`) decodes the ABI-encoded `address[]` argument via `extractBytes32Array`, which reads an attacker-controlled length word from the call data and immediately allocates `new byte[len][]` before any bound is enforced, mirroring the CVE-2024-30916 pattern where a crafted count/size field (`max_samples` in FastDDS DurabilityService QoS) is used unchecked to size an internal structure, causing DoS/resource exhaustion.

### Finding Description
`extractBytes32Array` reads the element count straight from the caller-supplied word and allocates an array of that size before any validation: [1](#0-0) 

`BatchValidateSign.doExecute` calls this method unconditionally to build the `addresses` array: [2](#0-1) 

The size cap (`MAX_SIZE = 16`) is only enforced inside the `if (VMConfig.allowTvmSelfdestructRestriction())` branch, which checks `sigArraySize`/`addrArraySize` *before* calling the extraction methods: [3](#0-2) 

When that VM feature flag is not active, `extractBytes32Array` is invoked with no upper bound on `len` at all — the value comes directly from `words[offset].intValueSafe()`, a caller-controlled 256-bit word truncated/clamped to an `int`. This lets a caller request an arbitrarily large `len` (up to `Integer.MAX_VALUE`), causing `new byte[len][]` to attempt to allocate hundreds of millions of array-reference slots (multi-GB) in a single call. This is directly analogous to the CVE-2024-30916 root cause: an untrusted "sample/element count" parameter used to size an internal container without validation, leading to a local resource-exhaustion DoS.

The outer `execute()` wraps `doExecute()` in `catch (Throwable t)`, which will catch the resulting `OutOfMemoryError`, but by the time the JVM throws it, the allocation attempt has already stressed the heap and triggered GC pressure across the whole node process — this doesn't need to permanently crash the node to be impactful; it degrades or stalls other in-flight VM executions and node operations for the duration of the GC/allocation attempt, and can crash the process depending on JVM heap headroom.

### Impact Explanation
Any account can trigger this by deploying/calling a contract (or an off-chain STATICCALL) invoking the `batchvalidatesign` precompile with a small, cheaply-crafted payload. Because `getEnergyForData` bases energy cost on `data.length` alone (not on the attacker-declared `len`), the attacker pays negligible energy for a huge in-JVM allocation attempt, giving an asymmetric DoS: minimal cost to the attacker, large memory/GC impact to the full node. This matches the "node crash or halt" / resource-exhaustion class explicitly allowed by the validation rules.

### Likelihood Explanation
Reachable from any signed transaction or constant call that hits the precompile address for `BatchValidateSign` — no special privilege required, and the vulnerable path is taken whenever `VMConfig.allowTvmSelfdestructRestriction()` is disabled (i.e., the legacy/default code path before that hardening flag activates, or on any historical/committee state where the feature is off).

### Recommendation
Enforce the same `MAX_SIZE` bound on the raw length word read inside `extractBytes32Array` (and `extractBytesArray`/`extractSigArray`) unconditionally, before allocating any array — not only inside the `allowTvmSelfdestructRestriction` gate — and validate that the declared length is consistent with the actual `words.length`/`data.length` before allocating.

### Proof of Concept
Not independently executed in this analysis (I could not run/build the project); the flow above is derived purely from static code reading of `PrecompiledContracts.java`. Conceptually: craft ABI-encoded input for `batchvalidatesign(bytes32,bytes[],address[])` where the offset word for the `address[]` parameter points to a word whose value is `0xFFFFFFFF` (or similarly large, clamped to `Integer.MAX_VALUE` by `intValueSafe()`), then call the precompile with `VMConfig.allowTvmSelfdestructRestriction()` disabled — `extractBytes32Array` will attempt `new byte[Integer.MAX_VALUE][]`, triggering an immediate large allocation/OOM.

**Note on uncertainty**: I was unable to inspect the full implementation of `DataWord.intValueSafe()` and the exact current runtime value of `VMConfig.allowTvmSelfdestructRestriction()` on mainnet before the final iteration cut off my tool access, so I cannot confirm the precise clamping behavior of `intValueSafe()` or whether this legacy code path is still reachable on current production chain parameters. This should be verified before treating the finding as confirmed exploitable.

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1156-1181)
```java
    private Pair<Boolean, byte[]> doExecute(byte[] data)
        throws InterruptedException, ExecutionException {
      if (VMConfig.allowTvmOsaka()
          && !isValidAbiEncoding(data, ABI_HEADER_WORDS, ABI_ITEM_WORDS)) {
        return Pair.of(false, EMPTY_BYTE_ARRAY);
      }
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
