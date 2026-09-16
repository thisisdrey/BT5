### Title
Unchecked native context handle in Sapling Shielded-Transfer precompiles allows JVM crash — (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The CVE describes `alloc_workqueue()`'s return value being used without a NULL check in `radeon_display.c`, causing a NULL-pointer dereference. The equivalent pattern exists in java-tron's Sapling shielded-transfer TVM precompiles: the native context handle returned by `JLibrustzcash.librustzcashSaplingVerificationCtxInit()` is never validated before being passed to subsequent native calls and to the corresponding "free" call, so a failed native allocation propagates an invalid handle straight into unmanaged/native code.

### Finding Description
`VerifyMintProof.execute()` calls `JLibrustzcash.librustzcashSaplingVerificationCtxInit()` and immediately uses the returned `long ctx` in `librustzcashSaplingCheckOutput`/`librustzcashSaplingFinalCheck`, then unconditionally calls `librustzcashSaplingVerificationCtxFree(ctx)` in a `finally` block, all without checking whether `ctx` is a valid pointer/handle. [1](#0-0) 

The JNI wrapper simply forwards the raw native return value with no validation layer: [2](#0-1) [3](#0-2) 

This precompile is directly reachable from an unprivileged `TriggerSmartContract` transaction hitting the corresponding precompile address via the TVM, and its energy cost is fixed (150,000) regardless of any internal native failure, meaning a caller pays a bounded, predictable fee to repeatedly exercise this path: [4](#0-3) 

If the underlying native allocation in the Rust/C `librustzcash` library ever fails (e.g., under memory pressure caused by concurrent shielded-proof verification load, since `VerifyTransferProof` spins up shared bounded thread pools that process attacker-supplied proof data concurrently), the returned context could be an invalid/null pointer. That invalid pointer is then dereferenced inside the native library by the subsequent check/free calls with no Java-side guard, which — unlike a Java NPE — manifests as a native-code crash of the entire JVM process (segfault), not a recoverable Java exception.

### Impact Explanation
A native-level NULL/invalid pointer dereference inside a JNI call cannot be caught by Java's `try/catch` (which only catches Java-level `Throwable`s); it crashes the JVM process outright. Since this precompile is reachable by any account submitting a `TriggerSmartContract` transaction that invokes the Sapling verify-mint/verify-spend precompile address, a full node (or all nodes, if replicated by the attacker across the network) could be halted, i.e., a denial-of-service / node crash, which is explicitly an accepted impact category (node crash or halt).

### Likelihood Explanation
Likelihood is limited because it hinges on inducing an actual native allocation failure inside `librustzcash`, which is not something an attacker can trivially trigger from a single call — similar to the CVE's own caveat that OOM at init time is unlikely. It would most plausibly occur only under sustained resource exhaustion via many concurrent shielded-proof precompile invocations from many transactions, or if the native library has additional failure paths that return a null handle (not confirmable from the Java side, since the native library source isn't in this repository/index).

### Recommendation
Validate the return value of `librustzcashSaplingVerificationCtxInit()` (and the analogous `librustzcashSaplingProvingCtxInit()`) immediately after the call in `PrecompiledContracts.VerifyMintProof/VerifySpendProof/VerifyOutputProof` (or centrally in `JLibrustzcash`), rejecting/short-circuiting the precompile call (returning the standard failure `Pair.of(true, DataWord.ZERO().getData())`) instead of proceeding to use or free an unchecked handle. This mirrors the recommended kernel fix of checking `alloc_workqueue()`'s return value before use.

### Proof of Concept
Not independently reproducible from the Java layer alone: the root cause depends on the native `librustzcash` library returning an invalid/null context under an out-of-memory or allocation-failure condition, which cannot be forced deterministically without native-level fault injection. The Java-side gap is confirmed by inspection — no null/zero check exists between `ctx = JLibrustzcash.librustzcashSaplingVerificationCtxInit()` and its subsequent use/free in `VerifyMintProof.execute()`. [5](#0-4)

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1381-1384)
```java
    @Override
    public long getEnergyForData(byte[] data) {
      return 150000;
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1394-1441)
```java
      boolean result;
      long ctx = JLibrustzcash.librustzcashSaplingVerificationCtxInit();
      try {
        byte[] cm = new byte[32];
        byte[] cv = new byte[32];
        byte[] epk = new byte[32];
        byte[] proof = new byte[192];
        byte[] bindingSig = new byte[64];
        byte[] signHash = new byte[32];
        byte[][] frontier = new byte[33][32];

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

        result = JLibrustzcash.librustzcashSaplingCheckOutput(
            new LibrustzcashParam.CheckOutputParams(ctx, cv, cm, epk, proof));
        long valueBalance = -value;
        result = result && JLibrustzcash.librustzcashSaplingFinalCheck(
            new LibrustzcashParam.FinalCheckParams(ctx, valueBalance, bindingSig, signHash));

        if (result) {
          byte[][] leafValue = new byte[1][32];
          System.arraycopy(cm, 0, leafValue[0], 0, 32);
          return insertLeaves(frontier, leafCount, leafValue);
        } else {
          return Pair.of(true, DataWord.ZERO().getData());
        }
      } catch (Throwable any) {
        String errorMsg = any.getMessage();
        if (errorMsg == null && any.getCause() != null) {
          errorMsg = any.getCause().getMessage();
        }
        logger.info("VerifyMintProof exception " + errorMsg);
      } finally {
        JLibrustzcash.librustzcashSaplingVerificationCtxFree(ctx);
      }
```

**File:** chainbase/src/main/java/org/tron/common/zksnark/JLibrustzcash.java (L162-164)
```java
  public static long librustzcashSaplingVerificationCtxInit() {
    return INSTANCE.librustzcashSaplingVerificationCtxInit();
  }
```

**File:** chainbase/src/main/java/org/tron/common/zksnark/JLibrustzcash.java (L200-202)
```java
  public static void librustzcashSaplingVerificationCtxFree(long ctx) {
    INSTANCE.librustzcashSaplingVerificationCtxFree(ctx);
  }
```
