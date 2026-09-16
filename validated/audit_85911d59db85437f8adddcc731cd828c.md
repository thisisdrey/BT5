### Title
Unbounded array-length allocation from attacker-controlled TVM precompile input can OOM/crash the node - ([File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java])

### Summary
`BatchValidateSign` and `ValidateMultiSign`, the precompiled contracts reachable from any TVM `CALL`/`STATICCALL` to their fixed addresses, decode caller-supplied ABI words into dynamic-array lengths and immediately allocate Java arrays of that size without validating it against any sane upper bound before the allocation happens.

### Finding Description
`extractBytes32Array` reads a length word straight from attacker-controlled calldata and allocates an array of that size before any validation: [1](#0-0) 

`extractBytesArray` has the same pattern — the `offset` bound is checked, but the `len` value taken from the word at that offset is used to size the allocation with no upper limit: [2](#0-1) 

`DataWord.intValueSafe()` only caps overflow to `Integer.MAX_VALUE`, it does not enforce any protocol-reasonable bound, so `len` can be crafted up to `2147483647`: [3](#0-2) 

In `BatchValidateSign.doExecute`, `extractBytes32Array` is invoked unconditionally with an offset derived purely from `words[2]`, and the only size guard (`MAX_SIZE`) is applied inside an `if (VMConfig.allowTvmSelfdestructRestriction())` branch — i.e. only when that hard-fork feature switch is active: [4](#0-3) 

The same conditional gating of the `MAX_SIZE` check exists in `ValidateMultiSign.execute`, where `extractBytesArray`/`extractSigArray` is called immediately after the guarded check, and the call itself sits outside any local try/catch for the surrounding method body: [5](#0-4) 

This mirrors the ImageMagick `ReadDIBImage` bug class: a length/size field taken directly from untrusted input is trusted and used to drive an allocation/copy before the value is sanity-checked, leading to a crash-class failure. Here the analog is a Java `OutOfMemoryError`/severe GC pressure triggered by a single crafted precompile call, rather than a native buffer overflow, but the root cause (unchecked attacker-controlled size feeding a resource-allocation operation) is the same.

### Impact Explanation
A precompile call can request an array allocation sized up to `Integer.MAX_VALUE` elements. Because java-tron runs all node responsibilities (block processing, P2P, API serving) inside a single JVM process, a large-enough single allocation attempt (or several enqueued in quick succession) can trigger `OutOfMemoryError` / heavy GC pauses that stall or crash the node process — a "node crash or halt" outcome, which is within the accepted impact categories. The severity is bounded by the fact that once the `allowTvmSelfdestructRestriction` proposal is activated network-wide, the `MAX_SIZE` (5/16) check runs before the allocation and neutralizes the issue for that specific chain-parameter state; the exposure exists only while that feature switch is not yet enabled (e.g., freshly bootstrapped private/test chains, or a mainnet window before the corresponding committee proposal is activated).

### Likelihood Explanation
Reachability requires only deploying/calling a contract that performs a `CALL`/`STATICCALL` to the `BatchValidateSign` or `ValidateMultiSign` precompile addresses with crafted calldata — no special privilege, SR/witness status, or network position is needed, matching the "unprivileged contract deployer/caller" threat model. The likelihood is conditioned on the chain not yet having `allowTvmSelfdestructRestriction` active; I could not fully verify from the available index whether this proposal is enabled by default on current java-tron mainnet configuration (the `DynamicPropertiesStore.java` default value for this flag was not found in the indexed content), so the real-world exploitability window is uncertain and should be confirmed against the live/default chain parameters.

### Recommendation
Add a hard maximum-length check (e.g., the same `MAX_SIZE` bound already used post-hardfork) unconditionally in `extractBytes32Array`, `extractBytesArray`, and `extractSigArray` before allocating `new byte[len][]`, rather than gating that protection behind the `allowTvmSelfdestructRestriction` feature switch. This removes any window (pre-activation networks, private/test nets, or chains that never enable the proposal) where the allocation size is attacker-controlled and unbounded.

### Proof of Concept
1. Deploy a contract that issues a low-level `call` to the `BatchValidateSign` precompiled address (0x...66 per TVM precompile map).
2. Construct calldata so that `words[2]` (the "addresses" array offset pointer) resolves, after `/WORD_SIZE`, to an in-bounds word whose value is set to `0x7FFFFFFF`.
3. On a chain/deployment where `allowTvmSelfdestructRestriction` is not yet active, `extractBytes32Array` executes `new byte[0x7FFFFFFF][]`, attempting a multi-gigabyte allocation in the node's single shared JVM heap, degrading or crashing the node process.

**Uncertainty note:** I was unable to confirm within the indexed code whether `allowTvmSelfdestructRestriction` is enabled by default on current java-tron mainnet, since `DynamicPropertiesStore.java`'s default-value logic for this specific flag wasn't retrievable via search. This should be verified before treating the finding as exploitable on production mainnet versus only on pre-activation/private chains.

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1051-1078)
```java
    @Override
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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1156-1177)
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
```

**File:** common/src/main/java/org/tron/common/runtime/vm/DataWord.java (L219-229)
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
