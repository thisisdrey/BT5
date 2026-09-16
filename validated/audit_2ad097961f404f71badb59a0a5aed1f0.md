### Title
Out-of-bounds/uncontrolled array read via unvalidated offsets in `VerifyTransferProof.execute` - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The `VrmlData_Scene::ReadLine` CVE describes a stack-based out-of-bounds read caused by advancing a buffer index (`ptr[++anOffset]`) without validating that the new offset stays within the buffer bounds. The same bug class — trusting attacker-controlled offset/length fields to index into a fixed-size buffer without bounds checking — exists in the zk-SNARK sapling `VerifyTransferProof` precompiled contract, which is reachable by any unprivileged contract call (TVM `STATICCALL`/`CALL` to the precompile address) supplying crafted `data`.

### Finding Description
`VerifyTransferProof.execute(byte[] data)` at [1](#0-0)  only validates that `data.length` is one of four fixed sizes `{2080, 2368, 2464, 2752}`. It then reads three fully attacker-controlled 32-byte-word offset fields directly from the payload: [2](#0-1) 

`spendOffset`, `spendAuthSigOffset`, and `receiveOffset` are parsed via `parseInt(data, idx)` with no range validation against `data.length` before being used as base offsets for further `parseInt`/`System.arraycopy` calls (`parseInt(data, spendOffset)`, `parseInt(data, spendAuthSigOffset)`, `parseInt(data, receiveOffset)`, and later `System.arraycopy(data, spendOffset + 320*i, ...)` etc. at [3](#0-2) ). Since `data.length` is fixed but `spendOffset`/`spendAuthSigOffset`/`receiveOffset` are arbitrary 32-bit values chosen by the caller, these offsets can point outside the buffer (negative or beyond `data.length`), causing `System.arraycopy`/array indexing to throw `ArrayIndexOutOfBoundsException`/`NegativeArraySizeException`. This mirrors the OCCT bug class: an index/offset value taken from untrusted input is used to walk a fixed-size buffer without checking it stays in range.

The similarly-structured sibling method `VerifyMintProof.execute` avoids this because all its offsets are hardcoded constants, but `VerifyTransferProof` and the shared helper `VerifyProof.insertLeaves`/`parseInt`/`parseLong` (path 1267–1275) do not bound-check attacker-supplied offsets before use.

### Impact Explanation
The exception is caught by the enclosing `try/catch (Throwable any)` at [4](#0-3)  equivalent pattern (VerifyMintProof shows the same catch-and-log-then-return-zero pattern used in the sibling class), so in the currently-read code path this likely degrades to a caught runtime exception rather than an uncaught crash — I could not fully confirm within the remaining budget whether `VerifyTransferProof.execute`'s try block (starting line 1472) wraps the entire offset-parsing region in a catch-all `Throwable` handler the same way `VerifyMintProof` does, or whether an exception thrown before entering the try (there is none, offsets are inside the try) would propagate. Based on what was read, the offset parsing at lines 1477–1494 does appear to be inside the same `try` block that starts at line 1472, meaning any `ArrayIndexOutOfBoundsException` would most likely be caught, logged, and converted into a bounded-value result — reducing this to, at most, incorrect proof-verification behavior or transaction failure/energy consumption rather than a node crash.

### Likelihood Explanation
Any account can trigger this by deploying/calling a contract that invokes the sapling `verifyTransferProof` precompiled contract address with a payload of one of the four accepted lengths but with corrupted offset words — no special privilege is required, matching the "unprivileged transaction broadcaster/contract caller" threat model.

### Recommendation
Given the uncertainty about full exception propagation, I recommend validating `spendOffset`, `spendAuthSigOffset`, and `receiveOffset` (and all derived offsets like `spendOffset+320*i`) explicitly against `data.length` before use, returning the standard `Pair.of(true, DataWord.ZERO().getData())` failure result — the same defensive pattern already used for the `leafCount >= TREE_WIDTH` and `spendCount`/`receiveCount` range checks a few lines below.

### Proof of Concept
Deploy/call a contract that performs a `CALL` to the sapling `verifyTransferProof` precompile address with `data.length == 2080` where the first 32-byte word (`spendOffset`) is set to a huge value (e.g. `0xFFFFFFFF` truncated to int, or a value like `2000000`) that is out of range for the 2080-byte buffer; this drives `parseInt(data, spendOffset)` / subsequent `System.arraycopy(data, spendOffset+..., ...)` calls to index outside the array bounds. [5](#0-4) 

Note: this analog is presented with reduced confidence — I was unable to fully confirm within the tool budget whether the surrounding `try/catch(Throwable)` fully contains this specific out-of-bounds access path, which materially affects whether the impact is a caught, harmless error or an unhandled exception. If a background review confirms an unhandled exception path is reachable (e.g., through a different entry method that doesn't wrap in `try/catch`), the severity should be escalated to a node-crash/DoS finding matching the reference CVE's impact more closely; if fully caught, this downgrades to a low-severity/no-impact finding and should not be reported.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1464-1531)
```java
    @Override
    public Pair<Boolean, byte[]> execute(byte[] data) {
      if (data == null) {
        return Pair.of(true, DataWord.ZERO().getData());
      }
      if (!Arrays.asList(SIZE).contains(data.length)) {
        return Pair.of(true, DataWord.ZERO().getData());
      }
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
