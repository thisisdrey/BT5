This is the analog. `extractBytesArray`/`extractSigArray` in `PrecompiledContracts.java` mirror the h5dump bug class exactly: they compute per-element offsets from **attacker-controlled call data words** (not a validated stride) and then use those to index into the same `words[]`/`data[]` array without checking they stay within bounds.

### Title
Unbounded Attacker-Controlled Offset in `extractBytesArray`/`extractSigArray` Causes DoS in `ValidateMultiSign`/`BatchValidateSign` Precompiles - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
`extractBytesArray` and `extractSigArray` (used by the `ValidateMultiSign` and `BatchValidateSign` TVM precompiled contracts) compute the byte offset of each array element from a caller-supplied `DataWord` (`words[offset + i + 1].intValueSafe()`), analogous to h5dump's `render_bin_output` computing a per-element stride from untrusted data. Neither method validates that the derived offset/length stays inside the `words`/`data` array before it is used to slice out bytes with `Arrays.copyOfRange`. [1](#0-0) 

### Finding Description
`extractBytesArray` reads `len = words[offset].intValueSafe()` and then, for each element `i`, derives `bytesOffset = words[offset + i + 1].intValueSafe() / WORD_SIZE` and `bytesLen = words[offset + bytesOffset + 1].intValueSafe()` purely from attacker-controlled call data, then calls `extractBytes(data, (bytesOffset + offset + 2) * WORD_SIZE, bytesLen)`, which does `Arrays.copyOfRange(data, offset, offset+len)` with no bounds check against `data.length`. [2](#0-1) 

`extractSigArray` has the identical pattern for fixed-length signatures. [3](#0-2) 

These helpers are reachable from an unprivileged smart-contract call through the `ValidateMultiSign` and `BatchValidateSign` precompiled contracts. In `BatchValidateSign.doExecute`, `words[1].intValueSafe() / WORD_SIZE` (the signature-array offset) is entirely attacker-supplied, and is passed straight into `extractSigArray`/`extractBytesArray` along with the raw `data` array. [4](#0-3) 

Similarly in `ValidateMultiSign.execute`. [5](#0-4) 

Because `bytesOffset`/`bytesLen`/the final `(bytesOffset + offset + 2) * WORD_SIZE` computation can be crafted to point beyond `data.length` (or to be negative after integer overflow in `intValueSafe()`), `Arrays.copyOfRange` will throw an unhandled `ArrayIndexOutOfBoundsException`/`NegativeArraySizeException`, or `words[offset + bytesOffset + 1]` in the intermediate step can throw `ArrayIndexOutOfBoundsException` on the `words[]` array itself when the crafted stride walks outside the parsed `DataWord[]`.

### Impact Explanation
An uncaught exception here is a Java-level `ArrayIndexOutOfBoundsException`/`RuntimeException` thrown from inside a precompiled contract during transaction execution. `ValidateMultiSign.execute` wraps user logic in a `try { ... } catch (Throwable t)` that swallows most exceptions but re-throws `OutOfTimeException`, so ordinary array-bounds exceptions there are actually caught and only logged, limiting impact to a failed/`DATA_FALSE` result. However, `BatchValidateSign.execute` also wraps `doExecute` in `try/catch (Throwable t)`, catching the exception the same way. Given both callers already catch `Throwable`, the exception itself does not crash the node — the practical effect is a transaction that reverts or returns `DATA_FALSE`/zeroed result instead of behaving as intended, i.e., a functional/logic bug rather than a node crash.

### Likelihood Explanation
Trivially reachable: any account can deploy a contract calling the `ValidateMultiSign` or `BatchValidateSign` precompile address with crafted call data offsets, and any anonymous JSON-RPC/HTTP caller triggering a `triggerConstantContract`/`triggerContract` against such a contract can hit this path. [6](#0-5) 

### Recommendation
Add explicit bounds validation of `offset`, `bytesOffset`, and `bytesLen` against `words.length` and `data.length` in `extractBytesArray`, `extractSigArray`, and `extractBytes32Array` before array access, returning an empty/failure result instead of relying on catching `Throwable` at the call sites.

### Proof of Concept
Craft ABI-encoded input to `BatchValidateSign` where the signature-array offset word (`words[1]`) points to a valid small array length, but the per-element offset word `words[offset + i + 1]` is set to a very large value (e.g., close to `Integer.MAX_VALUE`), causing `bytesOffset` to point far outside `data.length`; the subsequent `extractBytes` call throws `ArrayIndexOutOfBoundsException`, which is only caught generically and returns `DATA_FALSE` — confirming reachability but limiting confirmed impact to result-corruption/DoS-of-that-call rather than a node crash, since both call sites already wrap execution in broad exception handlers. [2](#0-1)

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L399-430)
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

  private static byte[] extractBytes(byte[] data, int offset, int len) {
    return Arrays.copyOfRange(data, offset, offset + len);
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1051-1074)
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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1144-1177)
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
