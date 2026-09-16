### Title
Uncontrolled Memory Allocation in `BatchValidateSign`/`ValidateMultiSign` Precompiles via Unvalidated ABI Array Length - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The TVM precompiled contracts `BatchValidateSign` (address `0x...09`) and `ValidateMultiSign` (address `0x...0a`) decode ABI dynamic arrays from raw call data by reading a 32-byte "length" word directly out of attacker-controlled input and using it to allocate a Java array (`new byte[len][]`) before any bound check is applied. This is the same bug class as CVE-2026-43868 (Thrift's "Memory Allocation with Excessive Size Value", CWE-789/CWE-1285): an untrusted, unvalidated size field drives a memory allocation.

### Finding Description
`extractBytesArray` and `extractBytes32Array` read the array length straight from calldata and immediately allocate: [1](#0-0) 

In `BatchValidateSign.doExecute`, the bound check (`MAX_SIZE = 16`) that would normally reject an oversized length is only performed when the `allowTvmSelfdestructRestriction` VM feature switch is active, and even then only gates the signature array — the address array is decoded unconditionally: [2](#0-1) 

The same pattern exists in `ValidateMultiSign.execute`, where `extractBytesArray` (the legacy/pre-TIP-854 path) is invoked before the `MAX_SIZE` check when the same feature flag is off: [3](#0-2) 

`intValueSafe()` only prevents overflow when converting a 256-bit `DataWord` to an `int`; it does not bound the value to a small range, so an attacker can set the ABI "array length" word inside the calldata to a large value (e.g. `0x7fffffff`). This directly reaches `new byte[len][]`, an allocation of up to ~2^31 object references (8–16 GB), long before the `MAX_SIZE`/`isValidAbiEncoding` guards (which are additionally gated behind the `allowTvmSelfdestructRestriction` / `allowTvmOsaka` feature switches) have a chance to reject it.

The energy cost for these precompiles (`getEnergyForData`) is derived only from `data.length`, not from the value encoded in the length word, so the energy metering does not throttle or reject this allocation attempt either: [4](#0-3) 

### Impact Explanation
Any account can trigger a smart contract that performs a `CALL`/`STATICCALL` to precompile address `0x...09` (`BatchValidateSign`) or `0x...0a` (`ValidateMultiSign`) with crafted calldata. Because these precompiles execute deterministically on every full node that validates/replays the transaction (per `Program.callToPrecompiledAddress`), a single transaction can force every validating node to attempt an outsized array allocation, producing an `OutOfMemoryError`/large GC pause. This is reachable purely by an unprivileged transaction broadcaster/contract caller and can degrade or crash node processes — a network-wide denial-of-service condition (CWE-789/CWE-1285), consistent with the scope's "node crash or halt" acceptance criterion.

### Likelihood Explanation
The vulnerable code path (unconditional `extractBytesArray`/`extractBytes32Array` calls without pre-validated length) is only closed once the `allowTvmSelfdestructRestriction` (TIP-854) and `allowTvmOsaka` feature switches are both activated on a given chain. On any deployment (private chain, test network, or a network where these specific TIPs have not yet been activated) the guard code is skipped entirely, making exploitation straightforward: it requires only crafting calldata for a `CALL` to a built-in precompile address with an oversized ABI length word — no special privileges, staking, or timing needed.

### Recommendation
- Validate the array-length word against a strict maximum (`MAX_SIZE`) and against the actual remaining calldata size **before** calling `extractBytesArray`/`extractBytes32Array`/`extractSigArray`, unconditionally (not gated behind a feature switch).
- Bound-check `len` against `Integer.MAX_VALUE / WORD_SIZE` and the real length of `data` before allocating `new byte[len][]`.
- Consider retroactively enforcing the TIP-854 guard logic regardless of `allowTvmSelfdestructRestriction`/`allowTvmOsaka` activation, since it addresses a memory-safety bug rather than a behavior change.

### Proof of Concept
1. Craft a `TriggerSmartContractContract` transaction that performs a `CALL` (or a plain low-level `data` call) to the `BatchValidateSign` precompile at TVM address `0000000000000000000000000000000000000000000000000000000000000009`, on a chain/version where `allowTvmSelfdestructRestriction` and `allowTvmOsaka` are not activated.
2. Build ABI-encoded calldata for `batchvalidatesign(bytes32,bytes[],address[])` where the offset word for `addresses` (`words[2]`) points to a length word set to a large value, e.g. `0x000000000000000000000000000000000000000000000000000000ffffffff`.
3. Submit/broadcast the transaction. During execution, `Program.callToPrecompiledAddress` invokes `BatchValidateSign.execute` → `doExecute`, which calls `extractBytes32Array(words, offset)` unconditionally; `len` is read from the crafted word, and `new byte[len][]` attempts to allocate a huge array, causing memory exhaustion/`OutOfMemoryError` on every node that executes the transaction.

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1066-1077)
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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1165-1177)
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
```
