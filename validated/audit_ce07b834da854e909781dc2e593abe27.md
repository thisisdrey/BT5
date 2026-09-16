## Finding: Unvalidated attacker-controlled offsets in `VerifyTransferProof.execute` cause out-of-bounds array access (analog to CVE-2017-17782)

### Title
Heap/array over-read via unvalidated `spendOffset`/`spendAuthSigOffset`/`receiveOffset` in shielded-transfer precompile - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
CVE-2017-17782 is a heap-based buffer over-read in GraphicsMagick's PNG chunk parser caused by using an attacker-supplied chunk length to index/allocate a buffer without validating it against the actual available data. The analogous bug class in java-tron is present in `VerifyTransferProof.execute`, the Sapling shielded-transfer TVM precompiled contract, where offsets read directly from the smart-contract call's `data` payload are used to index further into that same `data` array without any bounds checking against `data.length`.

### Finding Description
`VerifyTransferProof.execute` only checks that the overall `data.length` matches one of four fixed sizes [1](#0-0) . It then reads three fully attacker-controlled 32-byte "offset" fields directly from the payload: [2](#0-1) 

These `spendOffset`, `spendAuthSigOffset`, and `receiveOffset` values are used, unvalidated, to index back into `data` to read `spendCount`, `spendAuthSigCount`, and `receiveCount`: [3](#0-2) 

Only `spendCount`/`receiveCount` (values, not the offsets that produced them) are range-checked to be 1 or 2 [4](#0-3) . The offsets themselves are never checked to be within `[0, data.length)` before being used in `parseInt(data, spendOffset)` etc., and are reused again for the subsequent `System.arraycopy` loops that copy `nullifier`, `anchor`, `spendCv`, `rk`, `spendProof`, `spendAuthSig`, `receiveCm`, `receiveCv`, `receiveEpk`, `receiveProof` fields out of `data` at `spendOffset + 320*i + ...` and `receiveOffset + 288*i + ...` offsets [5](#0-4) .

This mirrors the GraphicsMagick root cause: a length/offset value taken from untrusted input is used to compute a read location/size without verifying it fits inside the actual buffer, producing an out-of-bounds read (in Java this manifests as an uncaught `ArrayIndexOutOfBoundsException` rather than silent memory disclosure, since the JVM performs bounds checking).

A structurally identical unbounded-offset pattern also exists in `extractBytesArray`/`extractBytes32Array`, which trust a length word (`words[offset].intValueSafe()`) taken from calldata to iterate `words[offset + i + 1]` without checking `offset + len` against `words.length` [6](#0-5) .

### Impact Explanation
Any contract deployer or transaction broadcaster can call the `VerifyTransferProof` precompile via a `TriggerSmartContract`/CALL to its fixed precompile address with a crafted 2080/2368/2464/2752-byte payload containing an out-of-range `spendOffset`, `spendAuthSigOffset`, or `receiveOffset`. This throws an unchecked exception deep inside proof-verification logic that spawns worker threads (`workersInConstantCall`/`workersInNonConstantCall`) for signature/proof verification. If the exception is not fully contained by the caller's exception handling for the executing node during transaction/contract execution, it can disrupt VM execution accounting (energy) and TVM execution flow, and — because native worker thread pools are involved — an uncaught exception on a pooled thread executing native Sapling verification calls (`JLibrustzcash`) risks leaving the executor/futures state inconsistent for concurrent shielded-transfer verifications on the node, an application-level denial-of-service condition on this specific execution path.

### Likelihood Explanation
The precompile is reachable directly from any signed transaction that triggers a smart contract CALL to the shielded-transfer precompile address; no special privileges are required, and the only prerequisite is that shielded TRC-20/Sapling opcodes are enabled on the network (a normal, already-active feature gate, not an attacker precondition). Constructing the malformed payload requires only picking large/negative offset words within the fixed total length, which is trivial.

### Recommendation
Validate `spendOffset`, `spendAuthSigOffset`, and `receiveOffset` are within `[0, data.length)` immediately after parsing them, and validate that every subsequent computed read range (`offset + fixedFieldSize*count`) stays within `data.length` before calling `parseInt`/`parseLong`/`System.arraycopy`, returning the standard `Pair.of(true, DataWord.ZERO().getData())` failure result instead of throwing. Apply the same offset/length bound check to `extractBytesArray` and `extractBytes32Array` (verify `offset + len` does not exceed `words.length` before the loop).

### Proof of Concept
Craft a 2080-byte payload (SIZE[0]) for the `VerifyTransferProof` precompile where the 32-byte word at offset 0 (`spendOffset`) is set to a value close to `Integer.MAX_VALUE` or larger than 2080 (e.g., `0x00000000000000000000000000000000000000000000000000000000000FFFFF`). Submit it as calldata in a `TriggerSmartContract` transaction that performs a low-level `CALL`/`STATICCALL` to the precompile's fixed address. `parseInt(data, spendOffset)` at [7](#0-6)  will attempt to read 32 bytes starting far past `data.length`, throwing `ArrayIndexOutOfBoundsException` inside the precompile execution path.

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1476-1479)
```java
        //parse unfixed field offset
        int spendOffset = parseInt(data, 0);
        int spendAuthSigOffset = parseInt(data, 32);
        int receiveOffset = parseInt(data, 64);
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1492-1494)
```java
        int spendCount = parseInt(data, spendOffset);
        int spendAuthSigCount = parseInt(data, spendAuthSigOffset);
        int receiveCount = parseInt(data, receiveOffset);
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1496-1499)
```java
        if (spendCount != spendAuthSigCount || spendCount < 1
            || spendCount > 2 || receiveCount < 1 || receiveCount > 2) {
          return Pair.of(true, DataWord.ZERO().getData());
        }
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1512-1531)
```java
        spendOffset += 32;
        for (int i = 0; i < spendCount; i++) {
          System.arraycopy(data, spendOffset + 320 * i, nullifier[i], 0, 32);
          System.arraycopy(data, spendOffset + 320 * i + 32, anchor[i], 0, 32);
          System.arraycopy(data, spendOffset + 320 * i + 64, spendCv[i], 0, 32);
          System.arraycopy(data, spendOffset + 320 * i + 96, rk[i], 0, 32);
          System.arraycopy(data, spendOffset + 320 * i + 128, spendProof[i], 0, 192);
        }
        spendAuthSigOffset += 32;
        for (int i = 0; i < spendCount; i++) {
          System.arraycopy(data, spendAuthSigOffset + 64 * i, spendAuthSig[i], 0, 64);
        }
        //output
        receiveOffset += 32;
        for (int i = 0; i < receiveCount; i++) {
          System.arraycopy(data, receiveOffset + 288 * i, receiveCm[i], 0, 32);
          System.arraycopy(data, receiveOffset + 288 * i + 32, receiveCv[i], 0, 32);
          System.arraycopy(data, receiveOffset + 288 * i + 64, receiveEpk[i], 0, 32);
          System.arraycopy(data, receiveOffset + 288 * i + 96, receiveProof[i], 0, 192);
        }
```
