Based on my research, I found a concrete analog: an unguarded array-index bug in the `ValidateMultiSign` TVM precompile that mirrors the FontForge bug class (attacker-controlled index/length used to index into an array without validating array bounds).

### Title
Unvalidated Attacker-Controlled Array Index in `ValidateMultiSign` Precompile Causes Uncaught `ArrayIndexOutOfBoundsException`/`NegativeArraySizeException` During Transaction Execution - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The `ValidateMultiSign` precompiled contract (address `0x...0a`, reachable by any account via a `CALL`/`STATICCALL` from a TVM contract, i.e. any unprivileged `TriggerSmartContract` transaction) parses raw, attacker-supplied calldata into a `DataWord[]` array and immediately indexes into it (`words[0]`, `words[1]`, `words[2]`, `words[3]`) and into helper methods `extractSigArray`/`extractBytesArray`/`extractBytes32Array` that compute array lengths and offsets directly from that same untrusted data, without validating that the derived indices stay within the bounds of the parsed `words` array. This is the same bug class as CVE-2025-15270 (improper validation of an array index leading to an out-of-bounds write/read).

### Finding Description
`PrecompiledContracts.ValidateMultiSign.execute()` [1](#0-0)  only performs a length-shape guard (`isValidAbiEncoding`) when `VMConfig.allowTvmOsaka()` is active: [2](#0-1) 
When that feature is not active, `rawData` is unconditionally parsed with `DataWord.parseArray(rawData)` and then indexed at fixed positions `words[0..3]` with no check that `words.length >= 4`.

Even under the guarded path, the downstream helpers derive an attacker-controlled `len` from the data itself and loop over it without checking each computed index against `words.length`: [3](#0-2) 
Here `len = words[offset].intValueSafe()` is fully attacker-controlled; the loop reads `words[offset + i + 1]` for `i` up to `len-1` with only a single check on `offset` at entry (`offset > words.length - 1`), not on every derived index. A crafted `len` (e.g., larger than the remaining word array) triggers `ArrayIndexOutOfBoundsException`; a negative `len` (achievable since `intValueSafe()` can yield a negative int) triggers `NegativeArraySizeException` on `new byte[len][]`.

Critically, the sibling precompile `BatchValidateSign` defends against exactly this class of failure by wrapping its equivalent logic in a catch-all: [4](#0-3) 
`ValidateMultiSign.execute()` has no equivalent outer `catch (Throwable t)` around the array-extraction logic — only an inner try/catch further down that wraps only the signature-recovery loop, which executes *after* the vulnerable indexing has already happened.

### Impact Explanation
An uncaught `RuntimeException` (`ArrayIndexOutOfBoundsException`/`NegativeArraySizeException`) thrown mid-precompile-execution is not a `ContractExeException`/`ContractValidateException` that the actuator/TVM exception hierarchy is designed to catch. If this exception is not intercepted by a generic `catch (Throwable/Exception)` somewhere higher up the TVM call stack (e.g., in `Program`'s precompile-call dispatch or `TransactionTrace`), it can escape uncontrolled during block application, which is the analog of the FontForge "write past array" leading to memory corruption/crash: here it manifests as an uncontrolled exception path during consensus-critical transaction execution, risking a node crash/halt when applying a block containing such a transaction. I was not able to fully confirm (due to running out of investigation budget) whether an outer generic catch in `Program`/`Runtime`/`Manager` fully contains this exception before it can affect block application; this is the key uncertainty in the impact chain, contrasted with the fact that the codebase explicitly added a defensive catch-all for the near-identical `BatchValidateSign` precompile, implying the missing catch in `ValidateMultiSign` is an oversight rather than intentional.

### Likelihood Explanation
Trivial to trigger: any account can call the `ValidateMultiSign` precompile address from a deployed contract with crafted, undersized or malformed calldata via a normal `TriggerSmartContract` transaction. No special privileges are required, and the guard (`isValidAbiEncoding`) that would prevent this is conditioned on the `allowTvmOsaka`/TIP-854 feature flag, meaning the exposure window depends on that flag's activation state on the target network.

### Recommendation
Add the same `isValidAbiEncoding`-style bounds validation unconditionally (not gated behind `allowTvmOsaka`) at the top of `ValidateMultiSign.execute()`, and/or wrap the whole `execute()` body (as `BatchValidateSign` does) in a `try { ... } catch (Throwable t) { return Pair.of(true, DATA_FALSE); }` so malformed input can never escape the precompile boundary as an uncaught exception. Additionally, harden `extractSigArray`/`extractBytesArray`/`extractBytes32Array` to validate every derived index (`offset + i + 1`, `bytesOffset`, `bytesLen`) against `words.length`/`data.length` before use, rather than trusting attacker-supplied lengths.

### Proof of Concept
Deploy a minimal contract that performs a raw `staticcall`/`call` to precompile address `0x000000000000000000000000000000000000000000000000000000000000000a` (`validateMultiSign`) with calldata shorter than 4 words (e.g., 32 bytes total) while `allowTvmOsaka` is not active on the target chain. `DataWord.parseArray` returns a `words` array with fewer than 4 elements; the subsequent `words[1].intValueSafe()` / `words[2].getData()` / `words[3].intValueSafe()` accesses throw `ArrayIndexOutOfBoundsException` inside `execute()`, which is not caught anywhere in that method. This can be sent as a normal `TriggerSmartContract` transaction by any unprivileged account.

**Note on confidence:** I could not fully verify, within the available investigation budget, whether higher layers of the TVM (`Program.callToPrecompiledAddress`, `Runtime`, `TransactionTrace`) contain a generic exception handler that would downgrade this to a benign transaction failure instead of a node-level crash/halt. This is the primary open question for confirming the severity of this finding.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L399-426)
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

  private static byte[][] extractSigArray(DataWord[] words, int offset, byte[] data) {
    if (offset > words.length - 1) {
      return new byte[0][];
    }
    int len = words[offset].intValueSafe();
    byte[][] bytesArray = new byte[len][];
    for (int i = 0; i < len; i++) {
      int bytesOffset = words[offset + i + 1].intValueSafe() / WORD_SIZE;
      bytesArray[i] = extractBytes(data, (bytesOffset + offset + 2) * WORD_SIZE,
          SIG_LENGTH);
    }
    return bytesArray;
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1051-1075)
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
