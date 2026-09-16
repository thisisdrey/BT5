### Title
Out-of-bounds array read / crash in `VerifyTransferProof.execute` via attacker-controlled offset fields - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The `VerifyTransferProof` precompiled contract (reachable from any TVM contract call, i.e. any unprivileged `TriggerSmartContract` transaction) parses several *unfixed* offset fields directly out of the caller-supplied `data` buffer and then uses those attacker-controlled values as `System.arraycopy` source offsets into that same buffer, without validating that the offsets/derived indices stay within `data.length`. This mirrors the ALPINE-CVE-2018-19841 bug class: a crafted binary/blob whose embedded length/offset fields are used unchecked, producing an out-of-bounds read.

### Finding Description
`VerifyTransferProof.execute(byte[] data)` only checks that `data.length` matches one of four fixed sizes: [1](#0-0) 

It then reads three offset fields directly from `data` with no range validation against `data.length`: [2](#0-1) 

`spendCount`, `spendAuthSigCount`, and `receiveCount` (also parsed from `data` at these attacker-influenced offsets) are bounds-checked only against a small range (`1` or `2`), but `spendOffset`, `spendAuthSigOffset`, and `receiveOffset` themselves are never validated to be `>= 0` and `< data.length`. They are then used directly as `System.arraycopy` source positions: [3](#0-2) 

Because `parseInt`/`parseLong` interpret arbitrary 32-byte words from `data` as signed integers (attacker fully controls the 2080–2752 byte payload), an attacker can set `spendOffset`, `spendAuthSigOffset`, or `receiveOffset` to a negative value or a value larger than `data.length`, causing `System.arraycopy` to throw `ArrayIndexOutOfBoundsException` (or `IndexOutOfBoundsException`) when reading past the end (or before the start) of the `data` array — the same “out-of-bounds read on malformed blob” root cause as the WavPack CVE.

### Impact Explanation
`VerifyTransferProof` is invoked as a TVM precompiled contract, callable by any account via a smart-contract call (`TriggerSmartContract`) that reaches this precompile address, i.e. by any unprivileged transaction broadcaster/contract deployer. While the top-level `catch (Throwable any)` in `execute` (and the mint-proof analog) does prevent the exception from crashing the node process outright, it does so for *this specific* code path; however, exceptions thrown asynchronously inside the submitted `Callable` tasks (`SaplingCheckSpendTask`, `SaplingCheckOutputTask`, `SaplingCheckBingdingSig`) that reference arrays built from unchecked offsets can surface through `Future.get()` and are also swallowed by the same generic catch — meaning the concrete impact is degraded to "silently returns a rejected proof" in the current code rather than an uncaught crash. Still, this exercises unchecked, attacker-controlled memory indexing in a component (Zcash/Sapling shield-transfer verification) that gates fund movement between shielded and transparent balances, so any latent path where the exception is not caught (e.g. thrown before entering the `try`, or thrown in a nested precompile without an equivalent catch) directly threatens node availability for that transaction handling thread. This is Medium severity: a reliably reachable, unauthenticated out-of-bounds memory access pattern in security-critical shielded-transaction verification code, matching the CVE's DoS/crash class, though its exploitability to a full node halt is currently mitigated by the enclosing `catch (Throwable)`.

### Likelihood Explanation
Likelihood is Medium: any account can call the `VerifyTransferProof` precompile via a smart contract with fully attacker-controlled 32-byte fields for `spendOffset`, `spendAuthSigOffset`, and `receiveOffset`; no privileged access is required. The only barrier to full impact is the existing broad `catch (Throwable)` in `execute`, which currently downgrades the crash to a swallowed exception/false result — an engineer must confirm whether all code reachable from these offsets (including the async worker tasks) is actually covered by that catch in every version/config.

### Recommendation
Add explicit bounds validation immediately after parsing `spendOffset`, `spendAuthSigOffset`, and `receiveOffset` (and any other data-derived offsets in `VerifyMintProof`/`VerifyTransferProof`/`VerifyBurnProof`), rejecting the call (returning a false/zero result) if any offset is negative or if `offset + requiredBytes > data.length`, mirroring the `verifyLength`-style bounds checks already used in `RLP.java` (`framework/src/main/java/org/tron/core/capsule/utils/RLP.java`) and `ContractEventParser.subBytes` (`framework/src/main/java/org/tron/common/logsfilter/ContractEventParser.java`).

### Proof of Concept
1. Deploy/call a contract that invokes the `verifyTransferProof` precompiled contract address with a `data` buffer of exactly one of the valid sizes (e.g. 2080 bytes).
2. Craft the first 32-byte word (`spendOffset`) to be a large positive value (e.g. `0x7FFFFFFF`) or a negative value (top bit set), and craft `spendCount`/`spendAuthSigCount`/`receiveCount` fields (read from `data[spendOffset]`, etc.) to pass the `1..2` range check trivially by having them coincidentally land on bytes within range — or alternatively set `spendAuthSigOffset`/`receiveOffset` similarly out of range.
3. Submit the transaction; observe that `parseInt(data, spendOffset)` / `System.arraycopy(data, spendOffset + 320*i, ...)` attempts to read outside the 2080-byte `data` array, throwing an `ArrayIndexOutOfBoundsException` that is caught by the generic `catch (Throwable any)` and logged, confirming the unchecked out-of-bounds access. [4](#0-3)

### Citations

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1476-1494)
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
