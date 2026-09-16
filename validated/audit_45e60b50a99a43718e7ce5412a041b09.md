Based on my investigation, I found a strong structural analog in the TVM precompiled-contract signature-verification code.

### Title
Out-of-bounds array read via unvalidated offset in `ValidateMultiSign` precompile - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
CVE-2017-16358 is an OOB read in radare2's `string_scan_range()` caused by scanning/indexing a buffer using an attacker-influenced length/offset without validating it against the buffer bounds before dereferencing. The same bug class — using an attacker-supplied word as a raw index into a decoded-word array before any bounds check — exists in `PrecompiledContracts.ValidateMultiSign.execute()`.

### Finding Description
`ValidateMultiSign.execute()` decodes calldata into a `DataWord[] words` array via `DataWord.parseArray(rawData)` [1](#0-0) . When the TIP `allowTvmSelfdestructRestriction` is active, the code computes an offset directly from attacker-controlled calldata and immediately indexes into `words` with it, before any bounds check is performed: [2](#0-1) 

Only *after* this raw, unguarded `words[words[3].intValueSafe() / WORD_SIZE]` access does the code call `extractSigArray`/`extractBytesArray`, which do contain a bounds check (`if (offset > words.length - 1) return new byte[0][];`) before touching the array [3](#0-2) . The sibling helper `extractBytes32Array`, used by `BatchValidateSign`, has *no* bounds check at all: [4](#0-3) .

This mirrors the radare2 root cause: a length/offset value taken from untrusted input is used to index into a buffer/array without first checking it against the array's actual size, producing an out-of-bounds read (`ArrayIndexOutOfBoundsException` in the JVM analog).

Notably, `BatchValidateSign.execute()` wraps its entire body in a defensive `try { return doExecute(data); } catch (Throwable t) { ... return Pair.of(true, new byte[WORD_SIZE]); }` specifically to swallow such exceptions [5](#0-4) , but `ValidateMultiSign.execute()` has no equivalent top-level guard — the unguarded `words[...]` access at line 1067 can throw directly out of `execute()`.

### Impact Explanation
`ValidateMultiSign` is a TVM precompile reachable by any smart contract via a `CALL`/`STATICCALL` opcode issued from any signed transaction or `triggerConstantContract` API request — no special privilege is required. If the raw indexing at line 1067 throws an uncaught `ArrayIndexOutOfBoundsException`, the behavior depends on whether the enclosing `Program`/actuator call stack generically catches runtime exceptions per-CALL (in which case impact is limited to that specific contract call failing/reverting) or lets it propagate further up transaction/block processing (in which case it could disrupt processing of a block containing the crafted transaction). I was not able to fully verify, within the remaining budget, how `Program.callToPrecompiledAddress` (or its caller) handles a raw `RuntimeException` versus the deliberately-caught path used in `BatchValidateSign`. The asymmetric defensive coding between the two nearly-identical precompiles (one guards against uncaught exceptions explicitly, the other does not) is itself evidence that an uncaught exception from this code path is considered unsafe by the codebase's own maintainers.

### Likelihood Explanation
High: crafting the calldata is trivial — the attacker only needs to set the `bytes[]` offset word (`words[3]`) to a value whose `/WORD_SIZE` exceeds `words.length - 1`, and enable the call only requires the `allowTvmSelfdestructRestriction` TIP to be active (a mainnet-activated feature, not attacker-controlled but broadly available), reachable from any account via a normal contract call to the precompile address for `validateMultiSign`.

### Recommendation
Add the same `if (offset > words.length - 1) { return Pair.of(true, DATA_FALSE); }` bounds check in `ValidateMultiSign.execute()` before the line `int sigArraySize = words[words[3].intValueSafe() / WORD_SIZE].intValueSafe();`, matching the guard already present in `extractBytesArray`/`extractSigArray`. Alternatively, wrap `ValidateMultiSign.execute()`'s body in the same defensive `try/catch(Throwable)` pattern used in `BatchValidateSign.execute()` for consistency and defense-in-depth.

### Proof of Concept
1. Enable/observe network state where `VMConfig.allowTvmSelfdestructRestriction()` is true (already active on production TIP-activated chains).
2. Deploy a trivial contract that issues a `CALL` to the `validateMultiSign(address,uint256,bytes32,bytes[])` precompile address.
3. Encode calldata where word index 3 (the offset for the `bytes[]` signatures array) is set to a large value, e.g. `0x00000000000000000000000000000000000000000000000000000000000fff * 32`, such that `words[3].intValueSafe() / WORD_SIZE` is greater than `words.length - 1` for the actual (short) calldata supplied.
4. Submit this as a transaction/`triggerConstantContract` call; observe that `words[offset]` in `ValidateMultiSign.execute()` (line 1067) throws `ArrayIndexOutOfBoundsException` before reaching the guarded `extractBytesArray`/`extractSigArray` helpers, unlike the equivalent `BatchValidateSign` path which is defensively caught.

**Uncertainty note:** I could not confirm within the available tool budget whether this uncaught exception is absorbed generically by `Program`/actuator-level exception handling (limiting impact to a reverted call) or propagates further (impacting block processing). This should be verified directly against `Program.callToPrecompiledAddress` and the actuator's transaction-execution exception handling before treating this as confirmed node-crash-level impact.

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1057-1057)
```java
      DataWord[] words = DataWord.parseArray(rawData);
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1066-1071)
```java
      if (VMConfig.allowTvmSelfdestructRestriction()) {
        int sigArraySize = words[words[3].intValueSafe() / WORD_SIZE].intValueSafe();
        if (sigArraySize > MAX_SIZE) {
          return Pair.of(true, DATA_FALSE);
        }
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
