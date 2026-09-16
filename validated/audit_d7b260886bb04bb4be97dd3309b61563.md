### Title
Out-of-bounds array read (`ArrayIndexOutOfBoundsException`) in `batchvalidatesign`/`validatemultisign`-style precompile parsing via `extractBytes32Array`/`extractBytesArray` - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
`PrecompiledContracts.extractBytes32Array` and `extractBytesArray` parse attacker-supplied precompile call `data` (a `DataWord[]` derived directly from calldata) using length/offset fields that are read from the calldata itself but never validated against the actual size of the `words` array before being used as array indices.

### Finding Description
`extractBytes32Array` reads `len = words[offset].intValueSafe()` and then loops `words[offset + i + 1]` for `i` in `[0, len)` with no check that `offset + len` stays within `words.length`. [1](#0-0) 
`extractBytesArray` performs a single bound check only for the initial `offset` (`if (offset > words.length - 1)`), but the derived `len`, `bytesOffset`, and `bytesLen` values are all taken from calldata-controlled `DataWord`s and used to index `words[offset + i + 1]` / `words[offset + bytesOffset + 1]` without any subsequent bounds validation. [2](#0-1) 
Because `words` is built by splitting the raw precompile call data into 32-byte `DataWord` chunks, a caller can craft a `len` value (e.g., a large positive int) that causes the loop to read past the end of the `words` array, throwing an unchecked `ArrayIndexOutOfBoundsException` deep inside TVM execution — the same fundamental bug class as the Exiv2 issue (parsing attacker-controlled structured data with insufficiently validated length/offset fields leading to out-of-bounds reads). A similar unguarded pattern also exists in the Sapling `VerifyTransferProof.execute` path, where `spendOffset`/`spendAuthSigOffset`/`receiveOffset` are parsed from `data` via `parseInt` and then used as absolute offsets into `data` for further `parseInt`/`arraycopy` calls without validating them against `data.length`. [3](#0-2) 

### Impact Explanation
An unhandled `ArrayIndexOutOfBoundsException` thrown from inside a precompiled contract call executed as part of transaction/contract execution is not necessarily fatal to the node process by itself in Java (it is a normal exception), but if it propagates outside the expected try/catch boundaries of the VM interpreter's opcode dispatch it can cause inconsistent handling of that specific transaction/energy accounting path, and in the worst case, if uncaught in a place expected to always produce a deterministic result, can lead to non-deterministic transaction outcomes across nodes (a consensus/chain-split style risk) rather than a simple revert. This matches the CVSS profile of the reference bug (availability impact via crafted, low-privilege-triggerable input), scoped here to a node/consensus availability concern rather than a memory-safety RCE since java-tron is JVM-based.

### Likelihood Explanation
Reachable directly by any unprivileged party who can send a transaction/contract call invoking these precompiles (e.g., interacting with the `batchvalidatesign`/`validatemultisign` precompile or the Sapling `verifyTransferProof` precompile) with malformed calldata containing an out-of-range length/offset word — no special privilege, prior state, or contract deployment beyond a normal signed transaction is required.

### Recommendation
Add explicit bounds checks in `extractBytes32Array`, `extractBytesArray`, and `extractSigArray` verifying that `offset + len` (and any derived `bytesOffset`/`bytesLen`) remain within `words.length` / `data.length` before use, returning an early failure result (consistent with existing `data == null`/length-mismatch handling) rather than allowing an unchecked exception to propagate. Apply the same offset-bounds validation to `spendOffset`, `spendAuthSigOffset`, and `receiveOffset` in `VerifyTransferProof.execute` before they are used to index into `data`.

### Proof of Concept
1. Construct a transaction calling the multisig-validation precompile (which uses `extractBytes32Array`/`extractBytesArray`) with calldata whose length-field word encodes a very large `len` (e.g., `0x7fffffff`) while the actual calldata is short.
2. Submit the transaction/contract call so it triggers precompile execution during TVM interpretation.
3. `extractBytes32Array`/`extractBytesArray` will attempt `words[offset + i + 1]` for `i` approaching `len`, exceeding `words.length` and throwing `ArrayIndexOutOfBoundsException` from within precompile execution, exercisable at will by any transaction sender.

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1476-1499)
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
        if (leafCount >= TREE_WIDTH - 1) {
          return Pair.of(true, DataWord.ZERO().getData());
        }

        int spendCount = parseInt(data, spendOffset);
        int spendAuthSigCount = parseInt(data, spendAuthSigOffset);
        int receiveCount = parseInt(data, receiveOffset);

        if (spendCount != spendAuthSigCount || spendCount < 1
            || spendCount > 2 || receiveCount < 1 || receiveCount > 2) {
          return Pair.of(true, DataWord.ZERO().getData());
        }
```
