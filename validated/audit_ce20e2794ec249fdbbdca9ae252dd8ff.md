## Finding

Root cause is analogous to the curl bug: an attacker-controlled 32-byte length field taken from untrusted data is used directly to size a memory allocation, with no upper bound, before any validation step runs.

### Title
Unbounded array allocation from attacker-controlled length in `ValidateMultiSign` precompile legacy path can crash the node - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
`PrecompiledContracts.ValidateMultiSign.execute()` reads the "signatures array length" word straight out of the caller-supplied `rawData` and, on the legacy (non-`allowTvmSelfdestructRestriction`) code path, passes it unchecked into `extractBytesArray`, which immediately does `new byte[len][]` before any size check is applied.

### Finding Description
In `execute()`: [1](#0-0) 

The `sigArraySize > MAX_SIZE` guard is only evaluated `if (VMConfig.allowTvmSelfdestructRestriction())`. When that hard-fork flag is not active, the ternary falls back to `extractBytesArray(words, words[3].intValueSafe() / WORD_SIZE, rawData)` with **no prior bound check** on the declared array length.

`extractBytesArray` then does: [2](#0-1) 

`len` comes from `words[offset].intValueSafe()`, i.e. directly from the calldata a caller supplies to the precompile (address `0x...a`). `new byte[len][]` is allocated *before* the subsequent `signatures.length > MAX_SIZE` check at line 1076-1078 can ever run — the oversized allocation happens first. This mirrors the curl flaw where a server-supplied block-size field is trusted enough to drive a `realloc()` before the result is validated.

The identical unguarded pattern also exists in `extractBytes32Array` (used by `BatchValidateSign`): [3](#0-2) 

Note: `BatchValidateSign.execute()` wraps its call in `try { return doExecute(data); } catch (Throwable t) { ... }` at [4](#0-3) , so a thrown `Error`/`RuntimeException` there is contained. **`ValidateMultiSign.execute()` has no such wrapping around the `extractBytesArray`/`extractSigArray` call** — the `try` block only starts later, around the account/permission lookup at line ~1082. An `OutOfMemoryError` or `NegativeArraySizeException` thrown while allocating `bytesArray`/`bytes32Array` therefore propagates uncaught out of `ValidateMultiSign.execute()`.

### Impact Explanation
A crafted smart-contract `CALL`/`STATICCALL` to the `ValidateMultiSign` precompiled address (`0x...a`) with a huge declared "signature count" word can force `new byte[len][]` to attempt a multi-gigabyte allocation. On typical JVM heap configurations this throws `OutOfMemoryError`, which is not caught anywhere inside `ValidateMultiSign.execute()`. Depending on how far up the VM call stack (block application in `Manager`/`TransactionTrace`) generically catches `Throwable`, this can at minimum abort processing of the transaction/block unexpectedly, and in adverse conditions can destabilize the JVM heap for the whole node process — a node crash/halt reachable from a single, unprivileged, signed transaction that deploys/triggers a contract calling this precompile.

### Likelihood Explanation
Reaching this code requires only a normal TRC-20/TVM contract that issues a `CALL` to precompile address `0x...a` (`validateMultiSign`) with attacker-chosen calldata — no special privileges, no consensus role, and no reliance on `allowTvmSelfdestructRestriction` being enabled (in fact the bug is only present when that flag is *not* yet active, i.e. legacy/default behavior on chains that haven't activated that particular TIP). This is directly reachable by any address that can broadcast a `TriggerSmartContract` transaction.

### Recommendation
Move the `sigArraySize > MAX_SIZE` (and equivalent) bound check so it always applies to the raw length word *before* `extractBytesArray`/`extractBytes32Array` performs `new byte[len][]`, regardless of the `allowTvmSelfdestructRestriction` flag state. Additionally, wrap the length-extraction calls in `ValidateMultiSign.execute()` in the same defensive `try/catch(Throwable)` pattern already used in `BatchValidateSign.execute()` so any residual allocation failure degrades to a rejected call instead of an uncaught error.

### Proof of Concept
Craft calldata for `validateMultiSign(address,uint256,bytes32,bytes[])` where the ABI word at the signatures-array length position (`words[words[3].intValueSafe()/32]`) is set to a very large value (e.g. `0x7fffffff`), while `VMConfig.allowTvmSelfdestructRestriction()` is not yet active on the target chain. Deploy a trivial contract that performs a `CALL` to address `0x...a` with this calldata and broadcast the triggering transaction; `extractBytesArray` will attempt `new byte[2147483647][]` before the `MAX_SIZE` check can reject it.

**Uncertainty note:** I was unable to confirm, within available tool calls, the exact clamping behavior of `DataWord.intValueSafe()` (i.e., whether it can return values as large as `Integer.MAX_VALUE` or whether it silently caps the range). This detail is central to how large the allocation attempt can actually get, and should be verified against `common/src/main/java/org/tron/common/runtime/vm/DataWord.java` before treating the PoC size as final.

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L399-404)
```java
  private static byte[][] extractBytesArray(DataWord[] words, int offset, byte[] data) {
    if (offset > words.length - 1) {
      return new byte[0][];
    }
    int len = words[offset].intValueSafe();
    byte[][] bytesArray = new byte[len][];
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
