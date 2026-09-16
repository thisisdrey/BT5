### Title
Unbounded array preallocation from attacker-controlled length word in precompiled-contract calldata parsing - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
`PrecompiledContracts.extractBytes32Array`, `extractBytesArray`, and `extractSigArray` read a 256-bit word directly from EVM calldata passed to a precompiled contract and use it, via `DataWord.intValueSafe()`, as the declared "length" of an array to preallocate — before any check that the calldata actually contains that many elements [1](#0-0) . This is structurally the same bug class as the go-ipld-prime CBOR issue: a size hint taken from an untrusted header/field is used directly for allocation sizing with no cap and no budget accounting tied to the declared value itself.

### Finding Description
`extractBytes32Array(DataWord[] words, int offset)` computes `int len = words[offset].intValueSafe();` and immediately does `byte[][] bytes32Array = new byte[len][];` before verifying `len` against the amount of data actually present in `words` [1](#0-0) . The same pattern repeats in `extractBytesArray` (`byte[][] bytesArray = new byte[len][];`) and `extractSigArray` (`byte[][] bytesArray = new byte[len][];`), each driven by an attacker-supplied word from calldata [2](#0-1) .

Only *after* the allocation does the code loop and index into `words[offset + i + 1]`, which is where an out-of-bounds access would eventually throw — but the large backing array (`new byte[len][]`, i.e., `len` object-reference slots) is allocated unconditionally beforehand. Since `len` comes straight from a 256-bit calldata word truncated by `intValueSafe()`, a caller can set it to a very large value (bounded only by whatever "safe" clamp `intValueSafe()` applies, plausibly up to `Integer.MAX_VALUE`) using calldata far smaller than that value would imply, exactly mirroring the CBOR advisory's core issue: a declared collection-size hint is used for preallocation without capping it or charging it against a budget before the collection is actually populated.

I was not able to fully confirm the exact call sites (which specific precompiled contract's `execute()` invokes these three helpers) or the precise upper bound enforced by `DataWord.intValueSafe()` within the available tool iterations — `common/src/main/java/org/tron/common/runtime/vm/DataWord.java` was located but its `intValueSafe()` body was not retrieved before the iteration budget ran out. This should be verified directly in the file before treating the exploitability as fully confirmed.

### Impact Explanation
If `intValueSafe()` does not cap the returned value to a small, bounded range, an unprivileged contract caller (any address that can send a transaction invoking a precompiled contract through a smart contract `CALL`) can force allocation of an array with hundreds of millions of reference slots from a calldata payload of only a few dozen bytes, potentially triggering an `OutOfMemoryError` and crashing/halting the node process — a denial-of-service reachable from ordinary transaction broadcasting, consistent with the CVSS impact (`A:H`) of the original advisory.

### Likelihood Explanation
Likelihood depends entirely on whether `DataWord.intValueSafe()` clamps to a reasonably small value or merely truncates a 256-bit word to a Java `int` (up to ~2.1 billion). If it merely truncates without a hard cap tied to actual data size, the attack requires only a single crafted transaction calling the relevant precompiled contract address with a manipulated length word — no special privilege, timing, or state is needed, making likelihood high if the truncation behavior is confirmed unbounded.

### Recommendation
Before allocating `new byte[len][]` (and similarly-sized structures) in `extractBytes32Array`, `extractBytesArray`, and `extractSigArray`, validate `len` against the actual available length of `words` (or the underlying `data` byte array) and reject/clamp before allocating, analogous to the go-ipld-prime fix that caps preallocation hints and charges the allocation budget as soon as the size is declared rather than only as entries are consumed.

### Proof of Concept
Conceptually: craft calldata to a precompiled contract whose `execute()` routes into `extractBytes32Array`/`extractBytesArray`/`extractSigArray`, setting the length word at the expected `offset` to a very large value (e.g., `0xFFFFFFFF` truncated by `intValueSafe()`) while keeping the rest of the calldata minimal. This should be validated by tracing which contract address dispatches to these helpers and confirming `intValueSafe()`'s clamp behavior in `common/src/main/java/org/tron/common/runtime/vm/DataWord.java`, which was not fully verified in this pass.

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
