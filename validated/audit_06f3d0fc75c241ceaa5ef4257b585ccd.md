### Title
Unbounded array allocation from attacker-controlled length word in TVM precompiles `ValidateMultiSign`/`BatchValidateSign` causes OOM/node crash - (`actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The `ValidateMultiSign` and `BatchValidateSign` precompiled contracts decode call data into `DataWord[]` and, on the legacy (non-`allowTvmSelfdestructRestriction`) code path, pass an attacker-controlled 256-bit word directly as an array-allocation size to `extractBytesArray`/`extractBytes32Array` before any bound is enforced. This is analogous to CVE-2017-14988: a length value taken straight from crafted input drives an oversized memory allocation, causing excessive memory use / OOM.

### Finding Description
`extractBytesArray` and `extractBytes32Array` read a length directly from the decoded call data and immediately allocate an array of that size: [1](#0-0) 

In `ValidateMultiSign.execute` and `BatchValidateSign.doExecute`, the size check (`sigArraySize > MAX_SIZE`) is only performed when `VMConfig.allowTvmSelfdestructRestriction()` is enabled; on the legacy branch, `extractBytesArray`/`extractBytes32Array` is invoked directly with no prior validation of the length word, and the `signatures.length > MAX_SIZE` guard only runs *after* the array has already been allocated: [2](#0-1) [3](#0-2) 

The optional `isValidAbiEncoding` check (only active under `VMConfig.allowTvmOsaka()`) merely validates that `data.length` is a multiple of the word size and consistent with a fixed header/item word count — it does not bound the actual length value read from an attacker-chosen offset word (`words[3]`/`words[1]`) that is used to index into `words[]` and then reinterpreted as an array size: [4](#0-3) 

Both `allowTvmSelfdestructRestriction` and `allowTvmOsaka` are runtime hard-fork flags gated by chain parameters/`ConfigLoader`, distinct from the older `allowTvmSolidity059` flag that merely gates whether these precompiles are reachable at all: [5](#0-4) [6](#0-5) 

The reachability of the precompile itself is controlled by the older/simpler flag: [7](#0-6) 

Because `words[offset].intValueSafe()` can return any value up to `Integer.MAX_VALUE` derived from a 256-bit word fully controlled by the caller's call-data, `new byte[len][]` (a reference array) can request an allocation on the order of gigabytes of heap (~16GB for `Integer.MAX_VALUE` references at 8 bytes/ref), well beyond any energy/CPU-time metering, because the JVM allocation happens synchronously inside `execute()` before any energy-based rejection can occur.

### Impact Explanation
Every full node that validates or replays the block containing such a transaction executes the same TVM code path deterministically, so a single crafted transaction can trigger simultaneous excessive memory allocation / `OutOfMemoryError` across the network, causing node crashes or halts (denial of service against block processing) on any node where the hardening flag (`allowTvmSelfdestructRestriction`) has not yet been activated on the chain. This matches the accepted impact category "node crash or halt."

### Likelihood Explanation
Reachable by any unprivileged account: deploy or call a contract that issues a `CALL`/`STATICCALL` to the `ValidateMultiSign` (`0x...a`) or `BatchValidateSign` (`0x...9`) precompile address with crafted call data where the length word at the referenced offset is set to a very large value. No special permission, staking, or witness/SR role is required — only that the network has not yet activated the `allowTvmSelfdestructRestriction` hard-fork flag (the flag exists specifically to backstop this exact code path via `MAX_SIZE`/pre-checks), meaning the fix is already partially in the codebase but only conditionally applied.

### Recommendation
Validate the length word against `MAX_SIZE` (or an absolute safety cap) in `extractBytesArray`/`extractBytes32Array`/`extractSigArray` unconditionally, before allocation, regardless of the `allowTvmSelfdestructRestriction` flag state, so legacy/pre-activation behavior cannot be abused for unbounded allocation. Alternatively, always take the `extractSigArray`/bounded path and remove the unconditional `extractBytesArray` fallback that has no pre-allocation size check.

### Proof of Concept
1. Deploy a contract that performs a `staticcall`/`call` to precompile address `0x...a` (`validateMultiSign`) with call data encoding: `address`, `permissionId`, `hash`, and a `bytes[]` array whose length-prefix word (referenced by `words[3]`) is set to a very large value (e.g., `0x7FFFFFFF`) instead of a legitimate small array length.
2. Ensure `allowTvmSelfdestructRestriction` is not yet active on the target chain (default/pre-hardfork state) — the legacy `extractBytesArray(words, words[3].intValueSafe() / WORD_SIZE, rawData)` path executes with no pre-check.
3. `extractBytesArray` executes `byte[][] bytesArray = new byte[len][];` with `len ≈ 0x7FFFFFFF`, immediately attempting a multi-gigabyte allocation and triggering `OutOfMemoryError` inside the node's TVM execution, before any energy/MAX_SIZE check is reached.

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L390-412)
```java
  private static byte[][] extractBytes32Array(DataWord[] words, int offset) {
    int len = words[offset].intValueSafe();
    byte[][] bytes32Array = new byte[len][];
    for (int i = 0; i < len; i++) {
      bytes32Array[i] = words[offset + i + 1].getData();
    }
    return bytes32Array;
  }

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L432-438)
```java
  private static boolean isValidAbiEncoding(byte[] data, int headerWords, int itemWords) {
    if (data == null || data.length % WORD_SIZE != 0) {
      return false;
    }
    long tail = subtractExact(data.length, multiplyExact(headerWords, WORD_SIZE));
    return tail > 0 && tail % multiplyExact(itemWords, WORD_SIZE) == 0;
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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1165-1181)
```java
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

**File:** common/src/main/java/org/tron/core/vm/config/VMConfig.java (L223-225)
```java
  public static boolean allowTvmSolidity059() {
    return current().allowTvmSolidity059;
  }
```

**File:** common/src/main/java/org/tron/core/vm/config/VMConfig.java (L303-309)
```java
  public static boolean allowTvmSelfdestructRestriction() {
    return current().allowTvmSelfdestructRestriction;
  }

  public static boolean allowTvmOsaka() {
    return current().allowTvmOsaka;
  }
```
