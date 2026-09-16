### Title
Unvalidated attacker-controlled data crossing the Java↔Rust (librustzcash) FFI boundary in TVM Sapling precompiles can crash the node - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`, `chainbase/src/main/java/org/tron/common/zksnark/JLibrustzcash.java`)

### Summary
The CVE describes a ClamAV DoS caused by unvalidated data crossing a C→Rust FFI boundary, where a crafted input reaches native Rust code and terminates the scanning process. java-tron has the same architectural pattern: the Sapling zk-SNARK precompiled contracts (`VerifyMintProof`, `VerifyTransferProof`, `VerifyBurnProof`, `MerkleHash`) copy attacker-controlled transaction/contract-call bytes into fixed-size buffers and pass them directly into the native `librustzcash` Rust library through the JNA binding `JLibrustzcash`/`Librustzcash`, with no field-level validation of curve points, proof components, or other cryptographic structures before the FFI call.

### Finding Description
`VerifyMintProof.execute`, `VerifyTransferProof.execute`, and `VerifyBurnProof.execute` in [1](#0-0)  and [2](#0-1)  only validate the overall byte-array length of the precompile input, then split the payload into fixed-size fields (`cm`, `cv`, `epk`, `proof`, `bindingSig`, `signHash`, etc.) with `System.arraycopy` and forward them unchecked to native functions such as `JLibrustzcash.librustzcashSaplingCheckOutput`, `librustzcashSaplingCheckSpend`, and `librustzcashSaplingFinalCheck`.

`VerifyTransferProof.execute` additionally derives internal offsets (`spendOffset`, `spendAuthSigOffset`, `receiveOffset`, `spendCount`, `receiveCount`) directly from attacker data via `parseInt`/`parseLong` at [3](#0-2)  before slicing the buffer, and then dispatches the parsed fields to native worker tasks (`SaplingCheckSpendTask`, `SaplingCheckOutputTask`, `SaplingCheckBingdingSig`) that call `JLibrustzcash.librustzcashSaplingCheckSpendNew`/`CheckOutputNew`/`FinalCheckNew` at [4](#0-3) .

`JLibrustzcash` is a thin JNA wrapper (`chainbase/src/main/java/org/tron/common/zksnark/JLibrustzcash.java`) that forwards these byte arrays straight into the native `Librustzcash` interface implementation with essentially no semantic validation of the cryptographic material (only a few call sites use `LibrustzcashParam.valid32Params`/`valid11Params` length checks) as seen at [5](#0-4) . This is the same FFI trust-boundary pattern flagged by CVE-2024-20380: Java-side code treats the buffers as opaque and defers correctness entirely to the native (Rust) side, catching only Java-level `Throwable`s (`catch (Throwable any)` in each `execute`), which cannot protect the JVM process from a native-side abort/panic that crosses the FFI boundary (e.g., a Rust panic unwinding into non-Rust frames, or a native `assert`/`abort` on malformed curve points), because such failures terminate the whole process rather than throwing a catchable Java exception.

### Impact Explanation
These precompiled contracts sit behind fixed TVM precompile addresses and are invocable by any account or smart contract via a normal `CALL`/`STATICCALL` to the corresponding precompile address, i.e., by an unprivileged contract caller submitting a single signed transaction — matching the "unauthenticated remote attacker" reach of the original CVE. If a native crash/abort is triggered inside `librustzcash` on crafted proof/curve-point bytes, the entire node process (super representative or full node) terminates, which is a network-wide denial-of-service condition; repeated exploitation could be used to force node crashes across the network whenever such a transaction propagates and is executed on each validating node.

### Likelihood Explanation
Exploitation requires constructing byte sequences of the exact expected lengths for `VerifyMintProof`/`VerifyTransferProof`/`VerifyBurnProof` inputs and encoding them as the `data` payload of a `CALL` to the corresponding precompile address inside a smart contract, which is achievable by any account able to broadcast a transaction (deploy or invoke a contract). No special privileges, keys, or consensus role are required; the Java-side wrapper only performs coarse length checks and duplicate-nullifier/output checks, not field-level (e.g., curve membership) validation, before invoking native code.

### Recommendation
- Add explicit semantic validation in Java for all Sapling proof components (curve point/group element membership, r/s style range checks) before crossing the FFI boundary, mirroring the defensive checks already applied in `P256Verify` (`actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java` lines 2362-2375).
- Audit the native `librustzcash`/JNA bridge to ensure `catch_unwind` (or equivalent) is used on the Rust side so panics cannot unwind across the FFI boundary and abort the JVM.
- Add fuzzing/regression tests targeting `VerifyMintProof`, `VerifyTransferProof`, and `VerifyBurnProof` with malformed/off-curve inputs to confirm no native crash occurs, only a graceful `Pair.of(true/false, ...)` failure result.

### Proof of Concept
1. Craft a TVM contract call to the `VerifyTransferProof` precompile address with a payload of the expected total length but with `cv`, `rk`, `epk`, or proof fields set to byte patterns that are invalid elliptic-curve encodings (e.g., points not on the curve, or values ≥ field modulus).
2. Broadcast a transaction invoking this precompile from any funded account (no special permission needed) — reachable through `VMActuator`/`Program` TVM execution as with any contract call.
3. If the native `librustzcash` implementation lacks internal validation for these fields and panics/aborts instead of returning an error code, the java-tron process terminates, demonstrating the DoS.

Note: full confirmation that the native Rust library actually panics/aborts (rather than gracefully returning `false`) on these inputs requires inspecting/testing the native `librustzcash` binary itself, which is outside the indexed Java source; the Java-side code shown provides no defense-in-depth against such a native failure regardless.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1377-1444)
```java
  public static class VerifyMintProof extends VerifyProof {

    private static final int SIZE = 1504;

    @Override
    public long getEnergyForData(byte[] data) {
      return 150000;
    }

    @Override
    public Pair<Boolean, byte[]> execute(byte[] data) {
      if (data == null) {
        return Pair.of(true, DataWord.ZERO().getData());
      }
      if (data.length != SIZE) {
        return Pair.of(true, DataWord.ZERO().getData());
      }
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
      return Pair.of(true, DataWord.ZERO().getData());
    }
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1477-1497)
```java
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
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1639-1729)
```java
      @Override
      public Boolean call() throws ZksnarkException {
        boolean result;
        try {
          result = JLibrustzcash.librustzcashSaplingCheckSpendNew(
              new LibrustzcashParam.CheckSpendNewParams(this.cv, this.anchor, this.nullifier,
                  this.rk, this.zkproof, this.spendAuthSig, this.signHash));
        } catch (ZksnarkException e) {
          throw e;
        } finally {
          countDownLatch.countDown();
        }
        return result;
      }
    }

    private static class SaplingCheckOutputTask implements Callable<Boolean> {

      private byte[] cv;
      private byte[] cm;
      private byte[] ephemeralKey;
      private byte[] zkproof;

      private CountDownLatch countDownLatch;

      SaplingCheckOutputTask(CountDownLatch countDownLatch, byte[] cv, byte[] cm,
          byte[] ephemeralKey, byte[] zkproof) {
        this.cv = cv;
        this.cm = cm;
        this.ephemeralKey = ephemeralKey;
        this.zkproof = zkproof;
        this.countDownLatch = countDownLatch;
      }

      @Override
      public Boolean call() throws ZksnarkException {
        boolean result;
        try {
          result = JLibrustzcash.librustzcashSaplingCheckOutputNew(
              new LibrustzcashParam.CheckOutputNewParams(this.cv, this.cm,
                  this.ephemeralKey, this.zkproof));
        } catch (ZksnarkException e) {
          throw e;
        } finally {
          countDownLatch.countDown();
        }
        return result;
      }
    }

    private static class SaplingCheckBingdingSig implements Callable<Boolean> {

      private long valueBalance;
      private int spendCvLen;
      private int receiveCvLen;
      private byte[] bindingSig;
      private byte[] signHash;
      private byte[] spendCvs;
      private byte[] receiveCvs;

      private CountDownLatch countDownLatch;

      SaplingCheckBingdingSig(CountDownLatch countDownLatch, long valueBalance, byte[] bindingSig,
          byte[] signHash, byte[] spendCvs, int spendCvLen,
          byte[] receiveCvs, int receiveCvLen) {
        this.valueBalance = valueBalance;
        this.bindingSig = bindingSig;
        this.signHash = signHash;
        this.spendCvs = spendCvs;
        this.spendCvLen = spendCvLen;
        this.receiveCvs = receiveCvs;
        this.receiveCvLen = receiveCvLen;
        this.countDownLatch = countDownLatch;
      }

      @Override
      public Boolean call() throws ZksnarkException {
        boolean result;
        try {
          result = JLibrustzcash.librustzcashSaplingFinalCheckNew(
              new LibrustzcashParam.FinalCheckNewParams(this.valueBalance, this.bindingSig,
                  this.signHash, this.spendCvs, this.spendCvLen,
                  this.receiveCvs, this.receiveCvLen));
        } catch (ZksnarkException e) {
          throw e;
        } finally {
          countDownLatch.countDown();
        }
        return result;
      }
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1732-1788)
```java
  public static class VerifyBurnProof extends VerifyProof {

    private static final int SIZE = 512;

    @Override
    public long getEnergyForData(byte[] data) {
      return 150000;
    }

    @Override
    public Pair<Boolean, byte[]> execute(byte[] data) {
      if (data == null) {
        return Pair.of(true, DataWord.ZERO().getData());
      }
      if (data.length != SIZE) {
        return Pair.of(true, DataWord.ZERO().getData());
      }
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
    }
  }
```

**File:** chainbase/src/main/java/org/tron/common/zksnark/JLibrustzcash.java (L166-211)
```java
  public static boolean librustzcashSaplingCheckSpend(CheckSpendParams params) {
    return INSTANCE.librustzcashSaplingCheckSpend(params.getCtx(), params.getCv(),
        params.getAnchor(), params.getNullifier(), params.getRk(), params.getZkproof(),
        params.getSpendAuthSig(), params.getSighashValue());
  }

  public static boolean librustzcashSaplingCheckOutput(CheckOutputParams params) {
    return INSTANCE.librustzcashSaplingCheckOutput(params.getCtx(), params.getCv(),
        params.getCm(), params.getEphemeralKey(), params.getZkproof());
  }

  public static boolean librustzcashSaplingFinalCheck(FinalCheckParams params) {
    return INSTANCE.librustzcashSaplingFinalCheck(params.getCtx(),
        params.getValueBalance(), params.getBindingSig(), params.getSighashValue());
  }

  public static boolean librustzcashSaplingCheckSpendNew(CheckSpendNewParams params) {
    return INSTANCE.librustzcashSaplingCheckSpendNew(params.getCv(),
        params.getAnchor(), params.getNullifier(), params.getRk(), params.getZkproof(),
        params.getSpendAuthSig(), params.getSighashValue());
  }

  public static boolean librustzcashSaplingCheckOutputNew(CheckOutputNewParams params) {
    return INSTANCE.librustzcashSaplingCheckOutputNew(params.getCv(), params.getCm(),
        params.getEphemeralKey(), params.getZkproof());
  }

  public static boolean librustzcashSaplingFinalCheckNew(FinalCheckNewParams params) {
    return INSTANCE
        .librustzcashSaplingFinalCheckNew(params.getValueBalance(), params.getBindingSig(),
            params.getSighashValue(), params.getSpendCv(), params.getSpendCvLen(),
            params.getOutputCv(), params.getOutputCvLen());
  }

  public static void librustzcashSaplingVerificationCtxFree(long ctx) {
    INSTANCE.librustzcashSaplingVerificationCtxFree(ctx);
  }

  public static boolean librustzcashIvkToPkd(IvkToPkdParams params) {
    return INSTANCE.librustzcashIvkToPkd(params.getIvk(), params.getD(), params.getPkD());
  }

  public static void librustzcashMerkleHash(MerkleHashParams params) {
    INSTANCE.librustzcashMerkleHash(params.getDepth(), params.getA(), params.getB(),
        params.getResult());
  }
```
