### Title
Unvalidated attacker-controlled field offsets in `VerifyTransferProof.execute()` cause out-of-bounds array access - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The `VerifyTransferProof` precompiled contract (Sapling shielded transfer proof verification, reachable via TVM `CALL` from any deployed contract) parses several "field offset" values directly out of attacker-supplied calldata and then uses them, unchecked, as byte-array indices into that same fixed-length calldata buffer. This mirrors the OpenEXR `ht_undo_impl()` bug class: a length/offset value declared inside the untrusted payload is trusted to index into a buffer without validating it against the buffer's actual bounds.

### Finding Description
`VerifyTransferProof.execute()` first validates only that the overall `data` length matches one of four fixed sizes [1](#0-0) . It then reads three offset fields directly from attacker-controlled calldata with no range validation against `data.length`: [2](#0-1) 

Those raw, unchecked offsets (`spendOffset`, `spendAuthSigOffset`, `receiveOffset`) are immediately used to index back into the same fixed-length `data` array via `parseInt(data, spendOffset)` etc.: [3](#0-2) 

Because `spendOffset`/`spendAuthSigOffset`/`receiveOffset` are fully attacker-controlled 32-bit values taken from the payload itself (not bounds-checked against `data.length`, which is at most 2752), a crafted call can set them to arbitrary large or negative values, causing `parseInt`/subsequent `System.arraycopy` calls (e.g. at lines 1514-1530 reading `spendOffset + 320*i + …`) to read outside the allocated `data` array — the same "declared-length-trusted-without-bounds-check" pattern as the reported OpenEXR HTJ2K decoder issue, just realized in Java as an out-of-bounds array access rather than a raw heap over-read.

A related, structurally identical pattern exists in `extractBytes32Array`/`extractBytesArray`, which iterate `len` (read from attacker-supplied `DataWord[]`) elements from `words[]` without checking `offset + len` against `words.length` [4](#0-3) .

### Impact Explanation
`VerifyTransferProof` is a precompiled contract invoked through the standard TVM `CALL` path (`PrecompiledContracts.getContractForAddress`), reachable by any account that can deploy or invoke a contract that calls this precompile address — i.e., an unprivileged transaction broadcaster. Because this code path executes identically on every full node/SR that replays the block containing such a transaction, an unhandled `ArrayIndexOutOfBoundsException`/`NegativeArraySizeException` thrown here during transaction execution is a deterministic condition triggered by consensus-critical code shared by all nodes. If this exception is not gracefully absorbed by the surrounding VM/precompile dispatch and results in inconsistent handling (e.g., only some nodes fail differently, or the process is not defensively guarded), it manifests as a reproducible crash/DoS condition for any node processing the malicious transaction — analogous to the "deterministic crash (DoS)" impact called out in the OpenEXR advisory.

### Likelihood Explanation
High — the precompile is reachable with a single crafted transaction from any account, no special privileges are required, and the calldata size checks at the top of `execute()` do nothing to validate the internal offset fields that are subsequently trusted as array indices.

### Recommendation
Validate `spendOffset`, `spendAuthSigOffset`, and `receiveOffset` (and every derived index) against `data.length` before use, mirroring the bounds checks already used elsewhere in this file (e.g., the `extractBytesArray` `offset > words.length - 1` guard). Apply the same review to `extractBytes32Array`/`extractBytesArray` to bound `len` against the actual `words.length` before iterating, and ensure any parsing exception in a precompiled contract's `execute()` is caught and converted into a normal (non-crashing) execution failure/revert rather than propagating as an uncaught runtime exception.

### Proof of Concept
1. Craft calldata of exactly one of the accepted lengths (2080/2368/2464/2752 bytes) for the `VerifyTransferProof` precompile address.
2. Set the 32-byte word at offset 0 (`spendOffset`) to a large value such as `0x7FFFFFFF` (or a value close to `Integer.MAX_VALUE`/negative when reinterpreted), while keeping `leafCount` and other prechecked fields within accepted ranges.
3. Deploy a trivial contract that performs `CALL` to the `VerifyTransferProof` precompiled address with this calldata, and broadcast the transaction.
4. On execution, `parseInt(data, spendOffset)` at [5](#0-4)  (or the subsequent `System.arraycopy` calls using `spendOffset + 320*i + …`) accesses `data[]` far outside its bounds, throwing an unchecked `ArrayIndexOutOfBoundsException` inside consensus-critical precompile execution.

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1466-1471)
```java
      if (data == null) {
        return Pair.of(true, DataWord.ZERO().getData());
      }
      if (!Arrays.asList(SIZE).contains(data.length)) {
        return Pair.of(true, DataWord.ZERO().getData());
      }
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1476-1487)
```java
        //parse unfixed field offset
        int spendOffset = parseInt(data, 0);
        int spendAuthSigOffset = parseInt(data, 32);
        int receiveOffset = parseInt(data, 64);
        System.arraycopy(data, 96, bindingSig, 0, 64);
        System.arraycopy(data, 160, signHash, 0, 32);
        //parse value
        long value = parseLong(data, 192);
        for (int i = 0; i < 33; i++) {
          System.arraycopy(data, i * 32 + 224, frontier[i], 0, 32);
        }
        long leafCount = parseLong(data, 1280);
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1492-1498)
```java
        int spendCount = parseInt(data, spendOffset);
        int spendAuthSigCount = parseInt(data, spendAuthSigOffset);
        int receiveCount = parseInt(data, receiveOffset);

        if (spendCount != spendAuthSigCount || spendCount < 1
            || spendCount > 2 || receiveCount < 1 || receiveCount > 2) {
          return Pair.of(true, DataWord.ZERO().getData());
```
