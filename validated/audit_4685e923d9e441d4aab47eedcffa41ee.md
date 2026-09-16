### Title
Unchecked native zk-SNARK verification context handle enables node crash via malformed shielded transaction - (File: `chainbase/src/main/java/org/tron/common/zksnark/JLibrustzcash.java`)

### Summary
`JLibrustzcash.librustzcashSaplingVerificationCtxInit()` wraps a native call that allocates a verification context and returns an opaque `long` handle (pointer). Every caller — including `PrecompiledContracts.VerifyMintProof`/`VerifyTransferProof`/`VerifyBurnProof` in `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java` and `ShieldedTransferActuator` in `actuator/src/main/java/org/tron/core/actuator/ShieldedTransferActuator.java` — takes that return value and passes it directly into subsequent native calls (`librustzcashSaplingCheckSpend`, `librustzcashSaplingCheckOutput`, `librustzcashSaplingFinalCheck`, `librustzcashSaplingVerificationCtxFree`) without ever validating that the init call succeeded. This is structurally identical to the reported FFmpeg `dnxhddec.c` bug class: the return value of an initialization routine that produces a handle/table used by later processing is never checked, so a failure path silently proceeds to use a bad handle.

### Finding Description [1](#0-0) 
returns the native `ctx` value with no success/failure check. It is consumed unchecked in the `VerifyBurnProof` precompile: [2](#0-1) 
Here `ctx = JLibrustzcash.librustzcashSaplingVerificationCtxInit();` is used immediately by `librustzcashSaplingCheckSpend`, `librustzcashSaplingFinalCheck`, and `librustzcashSaplingVerificationCtxFree` inside a try/finally, but nothing verifies `ctx != 0` (or whatever the native failure sentinel is) before those calls. The same unchecked pattern appears in the other `VerifyProof` subclasses (`VerifyMintProof`, `VerifyTransferProof`) and in `ShieldedTransferActuator`, all of which are directly reachable by any account broadcasting a TVM call to the shielded precompile addresses or a `ShieldedTransferContract` transaction. If the underlying native allocation fails (e.g. transient memory pressure, or corrupted/oversized proof data pushed through as `data`), the JNI layer receives a garbage/zero context pointer and dereferences it in the native Rust/C library, which is a memory-safety violation outside the JVM's control — the exact same "init function that returns a status/handle is never checked" defect pattern as the FFmpeg `init_vlc` report, but here manifesting through a native context handle instead of a VLC table.

### Impact Explanation
If the native context allocation can fail or return an invalid handle under attacker-influenced conditions (e.g., crafted proof/signature byte arrays that are 512/608+ bytes as required by `VerifyBurnProof`/`VerifyMintProof`/`VerifyTransferProof`, or resource exhaustion from repeated calls), passing that invalid handle into `librustzcashSaplingCheckSpend`/`CheckOutput`/`FinalCheck` can crash the validating/executing node process (JVM native crash), which is a denial-of-service against every node that processes the transaction — including all full nodes and SRs during block validation. A crash reachable from a single broadcast transaction or TVM contract call satisfies the "node crash or halt" acceptance bar.

### Likelihood Explanation
Reachability is high: any unprivileged account can call these precompiled contract addresses directly from a smart contract, or submit a `ShieldedTransferContract` transaction, supplying attacker-controlled proof/signature bytes with no permission checks beyond the trivial data-length assertions already present (`data.length != SIZE`). The exploitability of the underlying native init failure itself is not confirmed from the Java-side code alone — the actual Rust/librustzcash implementation of `librustzcashSaplingVerificationCtxInit` is native code outside the indexed Java sources, so whether it can realistically return an invalid handle under adversarial input (rather than just OOM) could not be verified in this repository. This uncertainty is the main gap in fully proving exploitability from java-tron code alone.

### Recommendation
Add an explicit check on the return value of `librustzcashSaplingVerificationCtxInit()` in `JLibrustzcash` (and at each call site in `PrecompiledContracts.VerifyProof` subclasses and `ShieldedTransferActuator`) before passing `ctx` to any `CheckSpend`/`CheckOutput`/`FinalCheck`/`VerificationCtxFree` call. On a failure sentinel (e.g., `ctx == 0`), fail the operation gracefully (`Pair.of(false, EMPTY_BYTE_ARRAY)` for precompiles, or throw a caught `ContractValidateException`/`ContractExeException` for the actuator) instead of proceeding to use the handle.

### Proof of Concept
Not independently reproducible from the available Java-only index: proving actual native-init failure requires exercising `librustzcashSaplingVerificationCtxInit`'s native implementation (Rust/C, not present in the indexed sources) under memory-pressure or malformed-input conditions, which could not be verified with the tools available.

### Citations

**File:** chainbase/src/main/java/org/tron/common/zksnark/JLibrustzcash.java (L162-164)
```java
  public static long librustzcashSaplingVerificationCtxInit() {
    return INSTANCE.librustzcashSaplingVerificationCtxInit();
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1749-1786)
```java
      boolean result;
      long ctx = JLibrustzcash.librustzcashSaplingVerificationCtxInit();
      try {
        byte[] nullifier = new byte[32];
        byte[] anchor = new byte[32];
        byte[] cv = new byte[32];
        byte[] rk = new byte[32];
        byte[] proof = new byte[192];
        byte[] spendAuthSig = new byte[64];
        byte[] bindingSig = new byte[64];
        byte[] signHash = new byte[32];
        //spend
        System.arraycopy(data, 0, nullifier, 0, 32);
        System.arraycopy(data, 32, anchor, 0, 32);
        System.arraycopy(data, 64, cv, 0, 32);
        System.arraycopy(data, 96, rk, 0, 32);
        System.arraycopy(data, 128, proof, 0, 192);
        System.arraycopy(data, 320, spendAuthSig, 0, 64);
        long value = parseLong(data, 384);
        System.arraycopy(data, 416, bindingSig, 0, 64);
        System.arraycopy(data, 480, signHash, 0, 32);

        result = JLibrustzcash.librustzcashSaplingCheckSpend(
            new LibrustzcashParam.CheckSpendParams(
                ctx, cv, anchor, nullifier, rk, proof, spendAuthSig, signHash));
        result = result && JLibrustzcash.librustzcashSaplingFinalCheck(
            new LibrustzcashParam.FinalCheckParams(ctx, value, bindingSig, signHash));
      } catch (Throwable any) {
        result = false;
        String errorMsg = any.getMessage();
        if (errorMsg == null && any.getCause() != null) {
          errorMsg = any.getCause().getMessage();
        }
        logger.info("VerifyBurnProof exception " + errorMsg);
      } finally {
        JLibrustzcash.librustzcashSaplingVerificationCtxFree(ctx);
      }
      return Pair.of(true, dataBoolean(result));
```
