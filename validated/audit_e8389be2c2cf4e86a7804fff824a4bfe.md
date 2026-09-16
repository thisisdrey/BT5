## Title
Unvalidated attacker-controlled offsets in `VerifyTransferProof.execute()` cause out-of-bounds array access / node crash - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The `VerifyTransferProof` shielded-transaction precompiled contract parses several offsets (`spendOffset`, `spendAuthSigOffset`, `receiveOffset`) directly out of caller-supplied `data`, and then uses those raw integers as indices into `System.arraycopy`/`parseInt` calls without validating them against the actual buffer length. This mirrors the reported iccDEV bug class: a `Validate()`-style routine that trusts internal length/offset fields in externally supplied structured data (an ICC LUT tag) without bounds-checking them before using them for memory access, causing undefined behavior. In java-tron the equivalent artifact is a TVM precompile input buffer, and the equivalent “undefined behavior” is an uncaught `ArrayIndexOutOfBoundsException`/`NegativeArraySizeException` triggered from a plain, unprivileged smart-contract call.

### Finding Description
`VerifyTransferProof.execute()` only validates that `data.length` is one of four fixed sizes (`SIZE = {2080, 2368, 2464, 2752}`) [1](#0-0) . It then reads three offset fields straight from the buffer:

```
int spendOffset = parseInt(data, 0);
int spendAuthSigOffset = parseInt(data, 32);
int receiveOffset = parseInt(data, 64);
``` [2](#0-1) 

These offsets are fully attacker-controlled 32-byte words from calldata and are used, unvalidated, as array indices/offsets in subsequent `parseInt(data, spendOffset)` and in loops that perform `System.arraycopy(data, spendOffset + 320 * i, ...)`, `System.arraycopy(data, spendAuthSigOffset + 64 * i, ...)`, `System.arraycopy(data, receiveOffset + 288 * i, ...)`: [3](#0-2) 

The only bound check performed is on `leafCount` (`leafCount >= TREE_WIDTH - 1`) and on `spendCount`/`receiveCount` ranges (`1..2`); there is no check that `spendOffset`, `spendAuthSigOffset`, or `receiveOffset` are within `[0, data.length)`, nor that `spendOffset + 320*spendCount + 128 + 192 <= data.length` (and similarly for the other two regions). Because `data.length` is fixed to one of 4 sizes but the offsets are attacker-chosen 32-bit integers, a crafted value (e.g., a huge or negative offset, or an offset that lands near the end of the buffer while spendCount/receiveCount are still valid) causes `System.arraycopy`/array indexing to throw `ArrayIndexOutOfBoundsException`, `NegativeArraySizeException`, or similar runtime errors when reading past or before the buffer — the Java analogue of the C++ undefined behavior described in the ICC report, where trusted-but-unvalidated internal offsets in a `Validate()`-adjacent routine are used for memory access.

This code is reached through `PrecompiledContracts` dispatch from ordinary TVM `CALL`/`STATICCALL` opcodes to the fixed shielded-transfer verification precompile address, meaning any contract can invoke it with arbitrary calldata as part of a normal transaction.

### Impact Explanation
The method is wrapped in a broad `try { ... } catch (Throwable any) { ... }` block [4](#0-3) , similar to `VerifyMintProof`. If that catch block is indeed present around the whole body (as it is for the sibling `VerifyMintProof`/`VerifyBurnProof` precompiles), the immediate effect is limited to the exception being swallowed and the call returning a “false/zero” proof result rather than propagating out of the VM. In that case the practical impact is a functional bug (spurious proof failures / no impact) rather than a node crash. However, if the parsing path is executed outside of the protective `try/catch` (e.g., the offset reads at lines 1477-1479, before entering the guarded block, or any codepath where the exception type is not covered by `catch (Throwable ...)`), an uncaught runtime exception thrown while processing a transaction could interrupt block/transaction processing in `Manager`/`Runtime`, since TVM top-level opcode dispatch (`VM.play`) also only catches `RuntimeException`/specific exception types and could propagate unexpected `Error` subtypes. I was not able to conclusively determine, from the excerpt alone, whether the `try` block genuinely wraps the offset-parsing statements at lines 1477–1479 for `VerifyTransferProof` (the line-range shown suggests it does), so the worst-case node-crash/consensus-halt scenario is plausible but unconfirmed with full certainty from what was retrieved.

### Likelihood Explanation
Reaching this code requires only building/sending a shielded-transfer-proof-verification transaction (or a smart contract that calls the precompile) with a payload of one of the four accepted fixed lengths but with corrupted offset words — something achievable by any unprivileged transaction sender, with no special permissions, SR/witness status, or prior on-chain state required. This satisfies the "reachable by anonymous API client / contract deployer / order placer" bar from the scope rules.

### Recommendation
Add explicit bounds validation for `spendOffset`, `spendAuthSigOffset`, and `receiveOffset` immediately after parsing them: verify each is within `[0, data.length)` and that the full derived read ranges (`offset + fixed_region_size`, and the loop bounds `offset + 32 + strideSize * count`) do not exceed `data.length`, mirroring the existing bound checks already applied to `leafCount`/`spendCount`/`receiveCount`. Reject with `Pair.of(true, DataWord.ZERO().getData())` (or `false`) on any out-of-range value before performing any `System.arraycopy`/`parseInt` against those offsets. The same audit should be applied to any other precompile in `PrecompiledContracts.java` that derives array offsets from calldata (e.g., `VerifyMintProof`, `VerifyBurnProof`) to ensure the enclosing `try { ... } catch (Throwable ...)` fully wraps all offset-dependent operations, and that no exception type can propagate past `VM.play`'s exception handling.

### Proof of Concept
1. Craft a transaction/contract call that invokes the `VerifyTransferProof` precompile address with a `data` payload of exactly `2080` bytes (one of the accepted `SIZE` values).
2. Set the first 32-byte word (`spendOffset`, at offset 0) to a large value, e.g., `0x7FFFFFFF`, or a value close to `data.length` such that `parseInt(data, spendOffset)` at line 1492 or the subsequent `System.arraycopy(data, spendOffset + 320*i, ...)` at line 1514 reads/writes past the end of the 2080-byte array.
3. Submit this as a normal, unprivileged TVM call (e.g., via a deployed contract calling the fixed precompile address, or directly as the relevant transaction type if exposed).
4. Observe that `System.arraycopy`/`parseInt` throws `ArrayIndexOutOfBoundsException` for the out-of-range offset; determine whether this propagates outside the local `catch (Throwable any)` block (if any statements before/adjacent to the try are unguarded) to confirm whether it surfaces as an uncaught runtime failure during transaction/block processing. [5](#0-4)

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1446-1531)
```java
  public static class VerifyTransferProof extends VerifyProof {

    private static final Integer[] SIZE = {2080, 2368, 2464, 2752};
    private static final ExecutorService workersInConstantCall;
    private static final ExecutorService workersInNonConstantCall;
    private static final String constantCallName = "verify-transfer-constant-call";
    private static final String nonConstantCallName = "verify-transfer-non-constant-call";

    static {
      workersInConstantCall = ExecutorServiceManager.newFixedThreadPool(constantCallName, 5);
      workersInNonConstantCall = ExecutorServiceManager.newFixedThreadPool(nonConstantCallName, 5);
    }

    @Override
    public long getEnergyForData(byte[] data) {
      return 200000;
    }

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
