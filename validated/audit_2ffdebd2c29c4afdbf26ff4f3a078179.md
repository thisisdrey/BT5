### Title
Missing length validation on Sapling shielded-transaction proof fields before JNI native calls enables node crash - (File: actuator/src/main/java/org/tron/core/actuator/ShieldedTransferActuator.java)

### Summary
`ShieldedTransferActuator.checkProof()` forwards attacker-controlled `SpendDescription`/`ReceiveDescription` byte fields directly into the native `librustzcash` JNI library (via `JLibrustzcash.librustzcashSaplingCheckSpend/CheckOutput/FinalCheck`) without verifying that these fields have the fixed lengths the Rust/C side expects. This is the same bug class as the `raw-cpuid` advisory: an "unsafe" native operation is exposed as if it were a safe function, without checking the precondition that its input matches the size/shape the underlying native code requires, so malformed input can hit a memory-safety fault in native code instead of failing gracefully in Java.

### Finding Description
In `checkProof()`, for each `SpendDescription` in an attacker-supplied `ShieldedTransferContract`, the code builds a `CheckSpendParams` directly from raw protobuf bytes (`valueCommitment`, `anchor`, `nullifier`, `rk`, `zkproof`, `spendAuthoritySignature`) and passes it straight into `JLibrustzcash.librustzcashSaplingCheckSpend`, with **no Java-side length check** on any of these fields: [1](#0-0) 

For `ReceiveDescription`, only `CEnc`/`COut` sizes are validated before the call to `librustzcashSaplingCheckOutput`; `valueCommitment`, `noteCommitment`, `epk` and `zkproof` are passed unchecked: [2](#0-1) 

The `FinalCheck` call similarly forwards `bindingSignature` and `signHash` without a length check: [3](#0-2) 

The underlying `JLibrustzcash` wrapper class shows the general pattern of exposing native `librustzcash` calls as plain Java static methods, sometimes with an explicit `LibrustzcashParam.valid32Params` precondition check (e.g. `librustzcashAskToAk`) and sometimes without any check at all before invoking the native `INSTANCE` methods: [4](#0-3) 

Because the Rust FFI boundary for Sapling proof verification (anchor/nullifier/rk/value-commitment = 32 bytes, zk-proof = 192 bytes, signatures = 64 bytes, per Zcash's Sapling spec) expects fixed-size buffers, an attacker who submits a `ShieldedTransferContract` with truncated or oversized byte fields (protobuf `bytes` fields impose no length constraint) can cause the JNI layer to read/write past the expected buffer bounds on the native side. This is analogous to `raw-cpuid`'s `cpuid_count()` calling an intrinsic without checking its safety precondition — the "safety contract" of the native call (fixed input sizes) is not enforced by the Java caller.

### Impact Explanation
A native buffer over-read/over-write triggered from Rust/C code embedded in the node process can cause a segmentation fault or memory corruption, crashing the entire `FullNode`/`SolidityNode` process. Because `ShieldedTransferContract` validation runs during ordinary transaction broadcast and validation (in `validate()`, which every node must execute to accept the transaction into its mempool/block), a single malicious signed transaction from any account holding minimal TRX for the shielded-transaction fee can crash every full node that processes it — a chain-wide denial-of-service. This matches "node crash or halt" in the accepted-impact list and CWE-400/CWE-657 (uncontrolled resource consumption / violation of secure design principles from unchecked native precondition).

### Likelihood Explanation
Likelihood is high in principle: the only requirements are that shielded transactions are enabled (`dynamicStore.supportShieldedTransaction()`) and same-token-name support is active — both are standard mainnet configurations — and the attacker crafts a `SpendDescription`/`ReceiveDescription` with a field (e.g., `zkproof`, `rk`, `anchor`, `nullifier`, `valueCommitment`) shorter or longer than the native code expects. The actual crash-triggering behavior depends on how the native `librustzcash` binding (not included in this repository, loaded via `LibrustzcashWrapper`) parses these byte slices — if it uses a checked/copy-into-fixed-array approach that panics safely on mismatched length, impact is reduced to a caught exception; if it treats the Java `byte[]` pointer/length blindly as a fixed-size native slice, a crash is likely. I could not inspect the native/Rust FFI implementation within this codebase to confirm the exact behavior on mismatched sizes, so this should be verified by fuzzing `librustzcashSaplingCheckSpend`/`CheckOutput`/`FinalCheck` with malformed-length inputs.

### Recommendation
Before constructing `CheckSpendParams`, `CheckOutputParams`, and `FinalCheckParams`, validate that every byte field matches its required fixed length (32 bytes for anchor/nullifier/rk/value-commitment/note-commitment/epk, 192 bytes for zk-proof, 64 bytes for signatures), mirroring the existing `ZC_ENCCIPHERTEXT_SIZE`/`ZC_OUTCIPHERTEXT_SIZE` checks and the `LibrustzcashParam.valid32Params` pattern already used elsewhere in `JLibrustzcash`. Reject the transaction with `ContractValidateException`/`ZkProofValidateException` on any size mismatch prior to invoking any native JNI call.

### Proof of Concept
1. Craft a `ShieldedTransferContract` with a valid `SpendDescription` except `zkproof` truncated to a shorter-than-192-byte array (protobuf `bytes` accepts arbitrary length).
2. Broadcast the transaction to a java-tron full node with shielded transactions enabled.
3. The node calls `ShieldedTransferActuator.validate()` → `checkProof()` → `JLibrustzcash.librustzcashSaplingCheckSpend(...)`, passing the malformed `zkproof` array straight into native code without a Java-side length check: [1](#0-0) 
4. If the native FFI binding reads a fixed number of bytes from the supplied pointer/length without validation, this can read out-of-bounds memory or corrupt the JVM's native heap, crashing the node process on every node that validates the transaction.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ShieldedTransferActuator.java (L293-305)
```java
        for (SpendDescription spendDescription : spendDescriptions) {
          if (!JLibrustzcash.librustzcashSaplingCheckSpend(
              new CheckSpendParams(ctx,
                  spendDescription.getValueCommitment().toByteArray(),
                  spendDescription.getAnchor().toByteArray(),
                  spendDescription.getNullifier().toByteArray(),
                  spendDescription.getRk().toByteArray(),
                  spendDescription.getZkproof().toByteArray(),
                  spendDescription.getSpendAuthoritySignature().toByteArray(),
                  signHash)
          )) {
            throw new ZkProofValidateException("librustzcashSaplingCheckSpend error", true);
          }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ShieldedTransferActuator.java (L308-322)
```java
        for (ReceiveDescription receiveDescription : receiveDescriptions) {
          if (receiveDescription.getCEnc().size() != ZC_ENCCIPHERTEXT_SIZE
              || receiveDescription.getCOut().size() != ZC_OUTCIPHERTEXT_SIZE) {
            throw new ZkProofValidateException("Cout or CEnc size error", true);
          }
          if (!JLibrustzcash.librustzcashSaplingCheckOutput(
              new CheckOutputParams(ctx,
                  receiveDescription.getValueCommitment().toByteArray(),
                  receiveDescription.getNoteCommitment().toByteArray(),
                  receiveDescription.getEpk().toByteArray(),
                  receiveDescription.getZkproof().toByteArray())
          )) {
            throw new ZkProofValidateException("librustzcashSaplingCheckOutput error", true);
          }
        }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ShieldedTransferActuator.java (L342-349)
```java
        if (!JLibrustzcash.librustzcashSaplingFinalCheck(
            new FinalCheckParams(ctx,
                valueBalance,
                shieldedTransferContract.getBindingSignature().toByteArray(),
                signHash)
        )) {
          throw new ZkProofValidateException("librustzcashSaplingFinalCheck error", true);
        }
```

**File:** chainbase/src/main/java/org/tron/common/zksnark/JLibrustzcash.java (L52-79)
```java
  public static void librustzcashCrhIvk(CrhIvkParams params) {
    INSTANCE.librustzcashCrhIvk(params.getAk(), params.getNk(), params.getIvk());
  }

  public static boolean librustzcashKaAgree(KaAgreeParams params) {
    return INSTANCE.librustzcashSaplingKaAgree(params.getP(), params.getSk(), params.getResult());
  }

  public static boolean librustzcashComputeCm(ComputeCmParams params) {
    return INSTANCE.librustzcashSaplingComputeCm(params.getD(), params.getPkD(),
        params.getValue(), params.getR(), params.getCm());
  }

  public static boolean librustzcashComputeNf(ComputeNfParams params) {
    INSTANCE.librustzcashSaplingComputeNf(params.getD(), params.getPkD(), params.getValue(),
        params.getR(), params.getAk(), params.getNk(), params.getPosition(), params.getResult());
    return true;
  }

  /**
   * @param ask the spend authorizing key,to generate ak, 32 bytes
   * @return ak 32 bytes
   */
  public static byte[] librustzcashAskToAk(byte[] ask) throws ZksnarkException {
    LibrustzcashParam.valid32Params(ask);
    byte[] ak = new byte[32];
    INSTANCE.librustzcashAskToAk(ask, ak);
    return ak;
```
