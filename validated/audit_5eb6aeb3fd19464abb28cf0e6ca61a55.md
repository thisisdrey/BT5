### Title
Single-signer weight amplification via ECDSA signature malleability in `ValidateMultiSign` precompile - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The `ValidateMultiSign` precompiled contract, reachable from any TVM smart contract via a `staticcall`/`call` to its precompile address, verifies an array of caller-supplied signatures against an on-chain `Permission`'s weighted key list and accepts the call once the accumulated weight reaches `permission.getThreshold()`. The de-duplication logic that is supposed to stop the same signer from being counted more than once only rejects a signature if the *recovered address* **and** the exact *raw signature bytes* were already seen; it does not deduplicate by signer identity alone. Because ECDSA signatures are malleable (multiple distinct byte encodings recover to the same address), a holder of a single low-weight key can supply several syntactically different but validly-recovering signatures for themselves and have their weight counted multiple times, bypassing the intended multi-party threshold — the same bug class as the reported "duplicate signature bypasses minimum signer requirement" issue.

### Finding Description
`ValidateMultiSign.execute` iterates the caller-supplied signature array, recovers an address per signature, and accumulates `weight` from `TransactionCapsule.getWeight(permission, recoveredAddr)`: [1](#0-0) 

```java
long totalWeight = 0L;
List<byte[]> executedSignList = new ArrayList<>();
for (byte[] sign : signatures) {
  byte[] recoveredAddr = recoverAddrBySign(sign, hash);
  sign = merge(recoveredAddr, sign);
  if (ByteArray.matrixContains(executedSignList, recoveredAddr)) {
    if (ByteArray.matrixContains(executedSignList, sign)) {
      continue;
    }
    MUtil.checkCPUTime();
  }
  long weight = TransactionCapsule.getWeight(permission, recoveredAddr);
  if (weight == 0) {
    return Pair.of(true, DATA_FALSE);
  }
  totalWeight += weight;
  executedSignList.add(sign);
  executedSignList.add(recoveredAddr);
}
if (totalWeight >= permission.getThreshold()) {
  return Pair.of(true, dataOne());
}
``` [2](#0-1) 

The check `matrixContains(executedSignList, recoveredAddr)` correctly detects that the *address* has already contributed, but the inner check `matrixContains(executedSignList, sign)` only `continue`s (skips re-counting) when the exact same raw signature blob is repeated. If the same key produces a second, byte-different-but-valid signature over the identical `hash` (trivially achievable through standard ECDSA signature malleability — e.g., re-encoding `(r, s, v)` as `(r, n-s, v')`, which both `ecrecover` to the same address, or by generating a fresh non-deterministic signature if the signer's engine is not using RFC6979), the code falls through past the `continue` (only invoking `MUtil.checkCPUTime()`, a CPU-time guard, not a rejection) and adds `weight` to `totalWeight` a second time for the *same key*.

This is architecturally identical to the reported Shardus bug: a threshold-of-signatures check that fails to deduplicate by signer identity, letting one signer's approval be replayed to satisfy a multi-party quorum. Here the "signers" are `Permission` keys and the "quorum" is `permission.getThreshold()`.

Contrast with the transaction-level signature check `TransactionCapsule.checkWeight`, which explicitly rejects repeated signers ("has signed twice!") via an address-keyed map: [3](#0-2) 

`ValidateMultiSign` was clearly intended to implement the same signer-uniqueness guarantee (hence the `executedSignList`/`matrixContains` machinery) but its condition is signature-blob-equality instead of signer-address-equality, leaving the malleability gap open.

### Impact Explanation
Any smart contract that gates fund transfers, withdrawals, or privileged operations behind `ValidateMultiSign` (the standard TRON multi-sig verification precompile used by custodial/multi-sig wallet contracts) can be tricked into approving an action using only a single, lower-weight key, by supplying several malleable variants of that key's signature in the `signs` array. This lets an attacker who controls one weak key in a multi-sig permission set unilaterally reach the configured `threshold`, resulting in unauthorized approval of operations — directly enabling theft of funds from any contract relying on this precompile for multi-party authorization.

### Likelihood Explanation
The precompile is directly callable by any account through an ordinary contract call (`TriggerSmartContract`); no privileged role or network position is required, matching the "unprivileged... contract call" reachability requirement. Producing a malleable alternate signature for the same message/key is a standard, well-documented ECDSA operation (bit/byte manipulation of `s`/`v`), requiring no cryptographic breakthrough — only possession of the attacker's own private key, which they already hold.

### Recommendation
In `ValidateMultiSign.execute`, deduplicate strictly by `recoveredAddr` (signer identity), not by the combination of address and raw signature bytes: once an address has contributed weight, any further signature recovering to that same address must be skipped (not merely CPU-time-checked), mirroring the address-keyed uniqueness enforcement already used in `TransactionCapsule.checkWeight`.

### Proof of Concept
1. Deploy a wallet contract (or any contract) that gates a fund-release function behind a call to the `ValidateMultiSign` precompile (`actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java:1036`), configured with a `Permission` containing keys `K1` (weight 1) and `K2` (weight 1) and `threshold = 2`.
2. As the holder of only `K1`, sign the target `hash` once normally to obtain signature `S1`.
3. Derive a second, distinct-but-valid signature `S1'` for the same `hash` under `K1` using ECDSA malleability (transform `s -> n - s` and flip the recovery id accordingly, or, if the signer does not use deterministic nonces, simply re-sign the same hash to get a different valid signature).
4. Call the target function passing `signatures = [S1, S1']`.
5. In the precompile loop: for `S1`, `recoveredAddr = K1`, not yet in `executedSignList` → weight 1 added, `(K1, S1)` recorded. For `S1'`, `recoveredAddr = K1` again — `matrixContains(executedSignList, K1)` is true, but `matrixContains(executedSignList, merge(K1, S1'))` is false (different bytes) → falls through, adds weight 1 again. `totalWeight = 2 >= threshold(2)` → precompile returns success (`dataOne()`), even though only `K1` ever signed.

<sub>Note: exact malleable-signature derivation utility functions (`recoverAddrBySign`, `merge`, `ByteArray.matrixContains`) were located by name but their full bodies were not retrieved in this session due to search scope; a Devin session with full file access can confirm the exact byte-level malleability transform accepted by `recoverAddrBySign`.</sub>

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1086-1106)
```java
            long totalWeight = 0L;
            List<byte[]> executedSignList = new ArrayList<>();
            for (byte[] sign : signatures) {
              byte[] recoveredAddr = recoverAddrBySign(sign, hash);

              sign = merge(recoveredAddr, sign);
              if (ByteArray.matrixContains(executedSignList, recoveredAddr)) {
                if (ByteArray.matrixContains(executedSignList, sign)) {
                  continue;
                }
                MUtil.checkCPUTime();
              }
              long weight = TransactionCapsule.getWeight(permission, recoveredAddr);
              if (weight == 0) {
                //incorrect sign
                return Pair.of(true, DATA_FALSE);
              }
              totalWeight += weight;
              executedSignList.add(sign);
              executedSignList.add(recoveredAddr);
            }
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1108-1110)
```java
            if (totalWeight >= permission.getThreshold()) {
              return Pair.of(true, dataOne());
            }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/TransactionCapsule.java (L242-263)
```java
    HashMap addMap = new HashMap();
    for (ByteString sig : sigs) {
      if (sig.size() < 65) {
        throw new SignatureFormatException(
            "Signature size is " + sig.size());
      }
      String base64 = TransactionCapsule.getBase64FromByteString(sig);
      byte[] address = SignUtils
          .signatureToAddress(hash, base64, CommonParameter.getInstance().isECKeyCryptoEngine());
      long weight = getWeight(permission, address);
      if (weight == 0) {
        throw new PermissionException(
            ByteArray.toHexString(hash) + " is signed by " + encode58Check(address)
                + " but it is not contained of permission.");
      }
      if (ForkController.instance().pass(Parameter.ForkBlockVersionEnum.VERSION_4_7_1)) {
        base64 = encode58Check(address);
      }
      if (addMap.containsKey(base64)) {
        throw new PermissionException(encode58Check(address) + " has signed twice!");
      }
      addMap.put(base64, weight);
```
