### Title
Unbounded Attacker-Controlled Length Field in `BatchValidateSign` Precompile Allocation Leads to Uncontrolled Memory Allocation / Node DoS - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The `BatchValidateSign` precompiled contract parses signature/address "array length" fields directly from attacker-controlled calldata and uses them to size a Java array (`new byte[len][]`) with no validated upper bound in the legacy code path, mirroring the iccDEV `CheckHeader()` bug class where a size/offset field taken from untrusted input is used unsafely before validation.

### Finding Description
`extractBytesArray()` and `extractBytes32Array()` read a length word directly from the parsed `DataWord[]` array and immediately allocate an array of that size: [1](#0-0) 

Note that `extractBytes32Array` performs no offset bounds check at all (unlike its sibling `extractBytesArray`, which at least checks `offset > words.length - 1`), and neither function bounds-checks the `len` value itself before allocating `new byte[len][]`.

These are invoked from `BatchValidateSign.doExecute()`: [2](#0-1) 

The `MAX_SIZE` (16) bound on `sigArraySize`/`addrArraySize` is only enforced when `VMConfig.allowTvmSelfdestructRestriction()` is active, and the word-alignment check via `isValidAbiEncoding()` is only enforced when `VMConfig.allowTvmOsaka()` is active: [3](#0-2) 

When either feature flag is disabled (e.g. on chains/configurations that haven't activated these later hard-fork gates), an attacker supplying calldata to this fixed-address precompile from any smart contract `CALL` can set the length word at an arbitrary offset to a value as large as `Integer.MAX_VALUE`, causing `new byte[len][]` to attempt to allocate billions of array-reference slots. Energy for this call is charged only based on `data.length / WORD_SIZE`: [4](#0-3) 

—i.e., the energy cost is completely decoupled from the size of the array the attacker forces the JVM to allocate, exactly analogous to iccDEV's `CheckHeader()` failing to validate a size field pulled from tag-table/offset data before using it in downstream computation.

### Impact Explanation
A crafted length field can force the JVM to attempt an oversized heap allocation for a shared, single-process TRON node. This can trigger `OutOfMemoryError` / heavy GC pressure across the whole node process (which also handles block application, P2P, and RPC), potentially destabilizing or crashing the node and denying service to legitimate API/RPC clients — this satisfies the "node crash or halt" / "API the node can no longer serve" impact bar. Because the energy metering does not scale with the attacker-chosen allocation size, the attack is cheap relative to its resource cost.

### Likelihood Explanation
Reachable from a single unprivileged, unauthenticated TVM contract call (`CALL`/`STATICCALL` to the `BatchValidateSign` precompile address) — no special privileges, SR/witness status, or victim interaction required. The only gating factor is whether `allowTvmSelfdestructRestriction`/`allowTvmOsaka` are active in the running network's parameters; on any deployment where these committee-controlled proposals are not yet enabled, the path is fully open.

### Recommendation
Enforce a strict, unconditional upper bound (e.g., `MAX_SIZE`) and offset/length sanity checks in `extractBytesArray()` and `extractBytes32Array()` regardless of `VMConfig` feature flags, before allocating any array, and reject with `Pair.of(false, EMPTY_BYTE_ARRAY)` on invalid input. Additionally, scale/verify energy cost against the actually-requested array size rather than solely `data.length`.

### Proof of Concept
1. Deploy a contract that performs a raw `CALL` (or use `staticcall` from Solidity) to the `BatchValidateSign` precompile address with calldata crafted such that:
   - `words[1]` (the signature-array offset pointer) points to a word position whose value (interpreted as `len`) is set to `0x7FFFFFFF`.
   - Word count is kept minimal so the true `data.length` is small (to minimize the declared/charged energy via `getEnergyForData`).
2. Invoke on a network/config where `allowTvmSelfdestructRestriction` and `allowTvmOsaka` are not active (so neither the `MAX_SIZE` check nor `isValidAbiEncoding` guard applies).
3. `extractBytesArray(words, offset, data)` executes `new byte[len][]` with `len = 0x7FFFFFFF`, forcing a massive allocation attempt in the node's JVM heap for a cost far below the actual resource impact, leading to `OutOfMemoryError`/GC thrash across the shared node process.

### Citations

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1137-1142)
```java
    @Override
    public long getEnergyForData(byte[] data) {
      long cnt = (data.length / WORD_SIZE - 5) / 6;
      // one sign 1500, half of ecrecover
      return cnt * ENGERYPERSIGN;
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
