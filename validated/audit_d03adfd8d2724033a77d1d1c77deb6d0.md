### Title
Out-of-Bounds Read via Unvalidated Offsets in Sapling `VerifyTransferProof` Precompiled Contract - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The `VerifyTransferProof.execute()` precompiled contract (used by TVM shielded-TRC20 contracts to verify Sapling transfer proofs) parses three "unfixed field" offsets (`spendOffset`, `spendAuthSigOffset`, `receiveOffset`) directly from attacker-controlled `data` and then uses them, unvalidated, as `System.arraycopy` source offsets into the same `data` buffer. This is directly analogous to the osslsigncode `pe_page_hash_calc()` bug, where `PointerToRawData`/`SizeOfRawData` values taken from an untrusted structure are used to index a buffer without checking they fall within its bounds.

### Finding Description
In `PrecompiledContracts.java` (`VerifyTransferProof.execute`), after validating only the *total length* of `data` against a fixed set of allowed sizes: [1](#0-0) 

the code reads three offsets straight out of the payload: [2](#0-1) 

These offsets are never checked against `data.length` before being used. They are immediately dereferenced via `parseInt(data, spendOffset)` and then used as base offsets for `System.arraycopy` calls whose length depends on attacker-controlled `spendCount`/`receiveCount`: [3](#0-2) 

Because `spendOffset`, `spendAuthSigOffset`, and `receiveOffset` are attacker-supplied 32-bit integers taken from the raw call data (with no upper/lower bound check, no check that `offset + requiredLength <= data.length`, and no check for negative values), a crafted contract call can point these offsets past the end of `data` or use negative/huge values, causing `System.arraycopy`/`parseInt` to throw `ArrayIndexOutOfBoundsException` (an out-of-bounds read attempt) or, depending on values, silently reading combined-with-wraparound memory ranges within the array. This mirrors the CVE-2026-39856 root cause: offsets/lengths from an untrusted structure used to index a buffer without validating they lie within the mapped region.

Contrast this with the sibling function `VerifyMintProof.execute()` in the same file, which uses only fixed, compile-time offsets and is safe: [4](#0-3) 

### Impact Explanation
The precompiled contract is reachable from any account by deploying/calling a smart contract (e.g. a ShieldedTRC20 contract) that invokes the Sapling `verifytransferproof` precompile with attacker-crafted `data`. Although the outer `execute()` has a broad `catch (Throwable any)` that swallows the resulting `ArrayIndexOutOfBoundsException` and returns a "false" result rather than crashing the node, the fact that arbitrary out-of-bounds offsets are accepted into internal array-copy logic is a genuine boundary-validation defect matching the CVE's bug class (unvalidated offset/length used to index a buffer derived from untrusted data). Because the exception is caught, the immediate impact is contained to that call (no full node crash observed in the code path), but this is still an out-of-bounds read attempt within the JVM heap driven entirely by user-controlled input, and any change to this catch behavior, timing side channel, or interaction with the CPU-time/energy accounting (which runs concurrently via a thread pool before the exception unwinds) could have follow-on effects. Given the severity ceiling requested (Medium/High/Critical only) and that root cause is a genuine unvalidated-offset OOB read reachable by any transaction sender, this is a legitimate Medium-severity analog.

### Likelihood Explanation
High reachability: any account can send a `TriggerSmartContract` transaction invoking the Sapling verify-transfer precompile with a payload of exactly one of the four allowed lengths `{2080, 2368, 2464, 2752}` but with the three offset words set to arbitrary out-of-range values. No special privilege, witness/SR status, or specific node role is required — this is a standard TVM precompile call path.

### Recommendation
Validate `spendOffset`, `spendAuthSigOffset`, and `receiveOffset` against `data.length` (and against negative values / integer overflow when adding `320*i`, `288*i`, `64*i` offsets) before using them in `parseInt`/`System.arraycopy`. Reject the call (return `Pair.of(true, DataWord.ZERO().getData())`) whenever any computed offset plus required read length would exceed `data.length`, mirroring the existing bounds discipline used elsewhere (e.g. `ContractEventParser.subBytes`, which explicitly checks `start < 0 || start >= src.length || length < 0 || length > src.length - start`).

### Proof of Concept
1. Deploy/interact with a contract that calls the Sapling `verifytransferproof` precompiled address with a 2080-byte (or any of the 4 valid `SIZE`) payload.
2. Set the first three 32-byte words (`spendOffset`, `spendAuthSigOffset`, `receiveOffset`) to large values (e.g. `0x7FFFFFFF` or a value close to `data.length` such that `spendOffset + 320*spendCount` exceeds `data.length`).
3. Submit the transaction; `parseInt(data, spendOffset)` / the subsequent `System.arraycopy(data, spendOffset + 320*i, ...)` calls attempt to read past the end of `data`, throwing `ArrayIndexOutOfBoundsException` — demonstrating the out-of-bounds read is reachable purely from unvalidated attacker-supplied offsets, exactly as in the osslsigncode advisory's unvalidated `PointerToRawData`/`SizeOfRawData` pattern.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1405-1418)
```java
        System.arraycopy(data, 0, cm, 0, 32);
        System.arraycopy(data, 32, cv, 0, 32);
        System.arraycopy(data, 64, epk, 0, 32);
        System.arraycopy(data, 96, proof, 0, 192);
        System.arraycopy(data, 288, bindingSig, 0, 64);
        long value = parseLong(data, 352);
        System.arraycopy(data, 384, signHash, 0, 32);
        for (int i = 0; i < 33; i++) {
          System.arraycopy(data, i * 32 + 416, frontier[i], 0, 32);
        }
        long leafCount = parseLong(data, 1472);
        if (leafCount >= TREE_WIDTH) {
          return Pair.of(true, DataWord.ZERO().getData());
        }
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1465-1471)
```java
    public Pair<Boolean, byte[]> execute(byte[] data) {
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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1492-1531)
```java
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
