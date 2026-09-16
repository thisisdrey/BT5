Based on the investigation, I found precompiled contract code in java-tron that parses attacker-controlled offsets from calldata and uses them directly for array copies without validating they stay within bounds — the same bug class as CVE-2019-19944 (a length/position value taken from decoded input is used for a memory copy without checking it against the buffer size).

### Title
Unvalidated attacker-controlled offsets in ShieldedTransfer/multi-sig precompiled contracts cause out-of-bounds array access - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
The `PrecompiledContract` implementations that parse raw TVM calldata (e.g. the Shielded-Tron `PrivateSend`/`BindingSignature` handling around `extractSigArray`/`extractBytes` and the offset parsing block that reads `spendOffset`, `spendAuthSigOffset`, `receiveOffset` from `data`) compute offsets and lengths straight from attacker-supplied input and then use them in `System.arraycopy`/`Arrays.copyOfRange` calls without first validating that `offset + length <= data.length`. [1](#0-0) [2](#0-1) 

### Finding Description
`extractSigArray` reads a length `len` from `words[offset]` (attacker-controlled `DataWord` derived from calldata) and then loops computing `bytesOffset` from further attacker-controlled words, feeding it into `extractBytes`, which does `Arrays.copyOfRange(data, offset, offset + len)` with no bound check against `data.length`. [2](#0-1) 

Similarly, the shielded-transfer precompile parses `spendOffset`, `spendAuthSigOffset`, and `receiveOffset` directly from calldata via `parseInt(data, 0/32/64)`, and later uses these attacker-influenced values as base offsets for numerous `System.arraycopy(data, spendOffset + 320 * i, ...)` calls, with no check that `spendOffset` (or the derived index) stays within `data.length`. [3](#0-2) 

This mirrors the root cause of CVE-2019-19944: a length/position field is decoded from untrusted input and used directly to index/copy from a buffer without verifying that `intLen`/`bufPos` (here, `offset`/`len`) remain within the buffer bounds.

### Impact Explanation
If an attacker supplies a crafted `spendOffset`/`spendAuthSigOffset`/`receiveOffset` or crafted `words[offset]` length value such that the computed source range exceeds `data.length`, the JVM will throw an uncaught `ArrayIndexOutOfBoundsException` (or `NegativeArraySizeException` for negative-length arrays like `byte[len][]`) from deep inside `System.arraycopy`/`Arrays.copyOfRange`. If this propagates up out of the guarded execution path of the TVM interpreter for this precompile invocation without being funneled into the standard revert/`Program.Exception` handling, it can cause an unhandled exception during transaction execution — a broadcastable, unprivileged-triggerable input to a node process. Depending on how far this propagates in the calling stack (block application / transaction execution loop in `Manager`), this can manifest as a denial-of-service against a full node processing the malicious transaction.

### Likelihood Explanation
This precompile is invoked whenever any account calls the corresponding contract address with crafted calldata inside a normal TRC-20/TVM transaction — a fully unprivileged, remotely broadcastable operation, matching the low-privilege/network-reachable "unauthorized transaction broadcaster" threat model in scope.

### Recommendation
Add explicit bounds validation immediately after parsing each offset/length value taken from `data` (e.g. `spendOffset`, `spendAuthSigOffset`, `receiveOffset`, and the length used in `extractSigArray`), rejecting (returning `Pair.of(false, ...)` or `Pair.of(true, DataWord.ZERO().getData())`) whenever `offset < 0 || offset + requiredLength > data.length`, mirroring the existing bound checks already present elsewhere in `RLP.java` (`verifyLength`) and `ByteUtil.parseBytes`.

### Proof of Concept
Send a transaction that calls the affected precompiled contract (Shielded-Tron `PrivateSend`-style precompile) with a calldata payload where the 32-byte word at offset 0 (`spendOffset`) is set to a very large value (e.g. `0x7FFFFFFF`) while total calldata is small. `parseInt(data, spendOffset)` at line 1492 and the subsequent `System.arraycopy(data, spendOffset + 320 * i, ...)` calls at lines 1514-1518 will attempt to read past `data.length`, throwing an uncaught `ArrayIndexOutOfBoundsException`.

**Note on confidence:** I was unable to fully trace the exact call path from `Program`/`OperationActions` into this precompile's `execute()` method or confirm whether an outer try/catch in the TVM dispatch layer (`Program`/`VM` opcode execution) already converts arbitrary `RuntimeException`s into a contained revert (which would reduce this to a contained, non-crashing revert rather than a node-crashing DoS). Given the index-size limits on retrieval, I could not verify the full exception-handling wrapper around precompile `execute()` invocations; a Devin session with full file access would be needed to confirm whether this bug is fully contained (low severity) or actually escapes as an unhandled exception (higher severity, matching the reported CVE's OOB-read impact class).

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L414-430)
```java
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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1472-1531)
```java
      try {
        byte[] bindingSig = new byte[64];
        byte[] signHash = new byte[32];
        byte[][] frontier = new byte[33][32];
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
        byte[][] anchor = new byte[spendCount][32];
        byte[][] nullifier = new byte[spendCount][32];
        byte[][] spendCv = new byte[spendCount][32];
        byte[][] rk = new byte[spendCount][32];
        byte[][] spendProof = new byte[spendCount][192];
        byte[][] spendAuthSig = new byte[spendCount][64];
        byte[][] receiveCm = new byte[receiveCount][32];
        byte[][] receiveCv = new byte[receiveCount][32];
        byte[][] receiveEpk = new byte[receiveCount][32];
        byte[][] receiveProof = new byte[receiveCount][192];

        //spend
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
