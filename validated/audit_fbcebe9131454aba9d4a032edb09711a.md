### Title
Unchecked native return value in `JLibrustzcash.librustzcashComputeNf` silently accepts failed nullifier computation - ([File: chainbase/src/main/java/org/tron/common/zksnark/JLibrustzcash.java])

### Summary
`JLibrustzcash.librustzcashComputeNf` discards the actual boolean result returned by the native `librustzcashSaplingComputeNf` call and unconditionally returns `true`, exactly mirroring the Nokogiri `xmlC14NExecute` bug class: a security/validity-relevant native call's return value is dropped, and every caller that relies on that boolean to detect failure is silently defeated.

### Finding Description
`JLibrustzcash.librustzcashComputeNf` is defined as: [1](#0-0) 

Unlike its sibling `librustzcashComputeCm`, which correctly returns the native call's result, `librustzcashComputeNf` calls `INSTANCE.librustzcashSaplingComputeNf(...)` and then hardcodes `return true;`, discarding whatever the JNI/JNA layer actually reported.

This is directly analogous to the Nokogiri report: `xmlC14NExecute`'s return code is discarded and an empty/garbage result is returned as if it succeeded, letting callers accept invalid canonicalized data. Here, if the native nullifier-computation call fails (e.g., due to malformed/edge-case key material, invalid diversifier, or other internal native error) the `result` byte buffer may remain unpopulated or contain stale/garbage bytes, yet the wrapper still tells the caller "success."

Every call site of `librustzcashComputeNf` is written expecting an honest boolean:
- `Note.nullifier(FullViewingKey, long)` and `Note.nullifier(byte[], byte[], long)`: [2](#0-1) 
- `Wallet.createShieldNullifier`: [3](#0-2) 
- `Wallet.getShieldedTRC20Nullifier`: [4](#0-3) 

All of these do `if (!JLibrustzcash.librustzcashComputeNf(...)) { return null; }` — but because the wrapper always returns `true`, this failure branch is dead code; it can never trigger regardless of what the native library actually did.

### Impact Explanation
The nullifier is the on-chain value that marks a shielded note as spent (`getNullifierStore().has(nf)` in `Wallet.isSpend`, and on-chain nullifier tracking for ShieldedTRC20 contracts). If the native computation silently fails and the caller proceeds with a garbage/zero-filled 32-byte buffer as though it were a valid nullifier, this could:
- Produce an incorrect or degenerate nullifier value used in transaction construction, `Wallet.isSpend`, or `getShieldedTRC20Nullifier`/`isShieldedTRC20ContractNoteSpent`, giving wrong "note spent" status to API/RPC callers.
- Be used downstream in `ShieldedTransferActuator.checkProof` / spend-proof generation paths that assume a validated nullifier accompanies a validated `cm`, weakening the invariant checked at `Spend is invalid` (`ShieldedTRC20ParametersBuilder.generateSpendProof` / `ZenTransactionBuilder.generateSpendProof`).

This falls into the "unbacked balance" / incorrect-validation category analogous to the SAML-bypass impact of the original report, but is scoped to shielded (Sapling/ShieldedTRC20) transaction nullifier computation, an anonymous-API- and transaction-broadcaster-reachable path (`CreateShieldNullifierServlet`, `Wallet.createShieldNullifier`, `IsShieldedTRC20ContractNoteSpent`, and internal spend-proof generation during shielded transfers).

### Likelihood Explanation
Likelihood is constrained by two factors I could not fully verify from the index:
1. Whether the underlying native `librustzcashSaplingComputeNf` (via `Librustzcash` JNA interface) is actually declared to return a `boolean` that can be `false` on any realistic input, or whether it always succeeds for well-formed 32/11-byte inputs (the interface/native declaration file was not found in the indexed code, only its consumer `JLibrustzcash.java`).
2. All observed inputs are pre-validated by `ComputeNfParams.valid()` (length/range checks) before the native call, which may make native-level failure rare for callers going through the public API. However, that does not eliminate the underlying defect — the wrapper still throws away a security-relevant signal regardless of how often it is actually exercised.

Because I cannot confirm from the available index whether the native function can genuinely fail for validated-but-adversarial inputs (e.g., crafted `nk`, `ak`, or diversifier combinations reachable through `CreateShieldNullifierServlet` or shielded transaction construction), I can only assess this as a plausible-but-unconfirmed medium-likelihood defect — the code pattern is a clear bug-class match to the reported CVE, but full exploitability depends on native-library internals not visible here.

### Recommendation
Fix `JLibrustzcash.librustzcashComputeNf` to return the actual boolean produced by the native call, matching the pattern used by `librustzcashComputeCm`:
```java
public static boolean librustzcashComputeNf(ComputeNfParams params) {
  return INSTANCE.librustzcashSaplingComputeNf(params.getD(), params.getPkD(), params.getValue(),
      params.getR(), params.getAk(), params.getNk(), params.getPosition(), params.getResult());
}
```
Additionally, audit all other `JLibrustzcash` wrappers (e.g. `librustzcashComputeNf`'s sibling `librustzcashKaAgree`, `librustzcashSaplingKaDerivepublic`, etc.) to confirm none of them similarly discard native return codes, and add regression tests asserting that a deliberately-failing native call surfaces as `false`/exception through `Note.nullifier`, `Wallet.createShieldNullifier`, and `Wallet.getShieldedTRC20Nullifier`.

### Proof of Concept
Not independently reproducible from static analysis alone since it requires triggering an actual native-library failure inside `librustzcashSaplingComputeNf` (JNA/Rust binding not present in the indexed Java sources). The code-level proof is the direct comparison of the two wrapper implementations:
- `librustzcashComputeCm` (correct): returns `INSTANCE.librustzcashSaplingComputeCm(...)` directly. [5](#0-4) 
- `librustzcashComputeNf` (defective): ignores `INSTANCE.librustzcashSaplingComputeNf(...)`'s return value and hardcodes `true`. [1](#0-0) 

A background Devin session with access to the native `Librustzcash`/JNA interface and the Rust `librustzcash` source could confirm under what conditions `librustzcash_sapling_compute_nf` returns `false` (e.g., invalid diversifier expansion) and construct a concrete PoC input that reaches this path via `CreateShieldNullifierServlet` / `Wallet.createShieldNullifier`.

### Citations

**File:** chainbase/src/main/java/org/tron/common/zksnark/JLibrustzcash.java (L60-63)
```java
  public static boolean librustzcashComputeCm(ComputeCmParams params) {
    return INSTANCE.librustzcashSaplingComputeCm(params.getD(), params.getPkD(),
        params.getValue(), params.getR(), params.getCm());
  }
```

**File:** chainbase/src/main/java/org/tron/common/zksnark/JLibrustzcash.java (L65-69)
```java
  public static boolean librustzcashComputeNf(ComputeNfParams params) {
    INSTANCE.librustzcashSaplingComputeNf(params.getD(), params.getPkD(), params.getValue(),
        params.getR(), params.getAk(), params.getNk(), params.getPosition(), params.getResult());
    return true;
  }
```

**File:** framework/src/main/java/org/tron/core/zen/note/Note.java (L196-214)
```java
  public byte[] nullifier(FullViewingKey vk, long position) throws ZksnarkException {
    byte[] ak = vk.getAk();
    byte[] nk = vk.getNk();
    byte[] result = new byte[32]; // 256
    if (!JLibrustzcash.librustzcashComputeNf(
        new ComputeNfParams(d.getData(), pkD, value, rcm, ak, nk, position, result))) {
      return null;
    }
    return result;
  }

  public byte[] nullifier(byte[] ak, byte[] nk, long position) throws ZksnarkException {
    byte[] result = new byte[32]; // 256
    if (!JLibrustzcash.librustzcashComputeNf(
        new ComputeNfParams(d.getData(), pkD, value, rcm, ak, nk, position, result))) {
      return null;
    }
    return result;
  }
```

**File:** framework/src/main/java/org/tron/core/Wallet.java (L2734-2745)
```java
    ComputeNfParams computeNfParams = new ComputeNfParams(
        paymentAddress.getD().getData(),
        paymentAddress.getPkD(),
        note.getValue(),
        note.getRcm().toByteArray(),
        ak,
        nk,
        incrementalMerkleVoucherContainer.position(),
        result);
    if (!JLibrustzcash.librustzcashComputeNf(computeNfParams)) {
      return null;
    }
```

**File:** framework/src/main/java/org/tron/core/Wallet.java (L4184-4196)
```java
    ComputeNfParams computeNfParams = new ComputeNfParams(
        paymentAddress.getD().getData(),
        paymentAddress.getPkD(),
        note.getValue(),
        note.getRcm().toByteArray(),
        ak,
        nk,
        pos,
        result);
    if (!JLibrustzcash.librustzcashComputeNf(computeNfParams)) {
      return null;
    }
    return result;
```
