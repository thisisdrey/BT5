## Analog Found

### Title
Unvalidated attacker-controlled array-length field in TVM precompile ABI decoding leads to unbounded allocation / out-of-bounds array access - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
The CVE describes an integer-overflow in a length field that is used to size a buffer without validating it against the actual available data, producing out-of-bounds access. The same bug class exists in java-tron's `BatchValidateSign`-style precompiled-contract input decoders `extractBytesArray` and `extractSigArray`, which trust a length word taken directly from attacker-supplied `calldata` to size an array before any bound-check against the real payload size.

### Finding Description
`extractBytesArray` and `extractSigArray` read a "count" value directly out of the raw ABI-encoded precompile input: [1](#0-0) 

```
int len = words[offset].intValueSafe();
byte[][] bytesArray = new byte[len][];
for (int i = 0; i < len; i++) {
  int bytesOffset = words[offset + i + 1].intValueSafe() / WORD_SIZE;
  int bytesLen = words[offset + bytesOffset + 1].intValueSafe();
  bytesArray[i] = extractBytes(data, (bytesOffset + offset + 2) * WORD_SIZE, bytesLen);
}
```

and the sibling function: [2](#0-1) 

`len` comes from `DataWord.intValueSafe()`, which only clamps an out-of-range 256-bit word to `Integer.MAX_VALUE` — it performs no correlation with the actual number of remaining 32-byte words in `words[]` or the size of `data`: [3](#0-2) 

The only prior gate is `isValidAbiEncoding`, which merely checks that `data.length` is a multiple of 32 bytes and that the "tail" size is a multiple of the declared item width — it does not verify that the length word embedded inside the payload actually matches that tail size: [4](#0-3) 

Because a caller fully controls the raw bytes passed to a precompiled contract, `words[offset]` can be crafted to hold a value up to `Integer.MAX_VALUE` (2,147,483,647) regardless of how many words actually follow. This causes:
1. `new byte[len][]` to attempt allocating an array of up to ~2.1 billion object references (≈16 GB on a 64-bit JVM with compressed oops disabled, or ≈8 GB with them enabled), which can throw `OutOfMemoryError`.
2. Even if allocation somehow succeeds partially, the very next iteration `words[offset + i + 1]` will run past the real `words.length` and throw `ArrayIndexOutOfBoundsException` — i.e., the same "out-of-bound access via crafted length field" bug class as the CVE, just in Java form.

### Impact Explanation
`OutOfMemoryError` is a JVM `Error`, not an `Exception`; unless every call site wraps precompile execution in a `catch (Throwable)` (only `recoverAddrBySign`'s ECRecover path shown here does so explicitly), an `OutOfMemoryError` thrown mid-transaction execution can propagate out of the transaction-processing thread and destabilize the node process, and even a caught OOM still leaves the JVM heap in a stressed/fragmented state that can affect concurrently processing transactions on the same node — a shared-resource denial-of-service vector. This is reachable via ordinary contract execution triggered by any account broadcasting a transaction that calls the corresponding precompiled contract (e.g., a batch signature-verification precompile), i.e., an unprivileged transaction broadcaster / contract caller, matching the "reachable by unprivileged actor" and "node crash or halt" criteria.

### Likelihood Explanation
High. No special privilege, staked resources beyond ordinary energy/bandwidth, or witness/SR role is required — an attacker only needs to deploy or invoke a contract that calls the vulnerable precompiled contract with crafted calldata containing an inflated length word. The vulnerable code path is purely arithmetic/array-indexing logic executed synchronously inside TVM opcode/precompile dispatch, so it is deterministically reachable from a single transaction.

### Recommendation
In `extractBytesArray` and `extractSigArray`, validate `len` against the actual bound before allocating: derive an upper bound from `(words.length - offset - 1)` (and/or the already-computed `itemWords`/`headerWords` used by `isValidAbiEncoding`) and reject/short-circuit (return `Pair.of(false, ...)`) when `len` exceeds that bound, instead of trusting the raw `intValueSafe()` value. Apply the same check to `extractBytes32Array`.

### Proof of Concept
1. Construct calldata for the vulnerable precompiled contract (the batch-signature-verification style contract that calls `extractBytesArray`/`extractSigArray`) such that the ABI "array length" word at the expected offset is set to a large value (e.g. `0x7fffffff`), while the rest of the payload contains only a few real words following it (payload otherwise passes `isValidAbiEncoding`'s length%32==0 / tail-alignment checks by padding as needed).
2. Deploy a minimal contract that performs a `STATICCALL`/`CALL` to the precompile address with this crafted calldata, and broadcast a transaction invoking it.
3. Observe that `extractBytesArray`/`extractSigArray` attempts `new byte[len][]` with `len` far exceeding the real data, resulting in `OutOfMemoryError` or `ArrayIndexOutOfBoundsException` inside the executing node's TVM thread.

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
