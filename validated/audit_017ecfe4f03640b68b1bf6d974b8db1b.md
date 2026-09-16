### Title
Signature-malleability double-counting bypasses multisig threshold in `ValidateMultiSign` TVM precompile - ([File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java])

### Summary
The `ValidateMultiSign` precompiled contract, reachable by any smart contract via a `TriggerSmartContract` call, deduplicates signer weights using an incomplete check: it only skips a signature if the exact byte sequence of `(recoveredAddr || sign)` has already been seen for that address. Because ECDSA signature verification in this codebase (`ECKey.ECDSASignature.validateComponents`) does not enforce canonical (low-S) signatures, a single private key can produce two distinct, both-valid signatures over the same hash (the canonical `s` and its malleable counterpart `N-s`, with `v` flipped). The precompile's dedupe logic lets the second, byte-different signature from the *same* signer pass through and add its weight a second time, inflating `totalWeight` and allowing a single key to satisfy a threshold that was supposed to require multiple independent signers.

### Finding Description
In `PrecompiledContracts.ValidateMultiSign.execute` [1](#0-0) , each provided signature is processed as follows:

```
byte[] recoveredAddr = recoverAddrBySign(sign, hash);
sign = merge(recoveredAddr, sign);
if (ByteArray.matrixContains(executedSignList, recoveredAddr)) {
  if (ByteArray.matrixContains(executedSignList, sign)) {
    continue;
  }
  MUtil.checkCPUTime();
}
long weight = TransactionCapsule.getWeight(permission, recoveredAddr);
...
totalWeight += weight;
executedSignList.add(sign);
executedSignList.add(recoveredAddr);
```

If `recoveredAddr` has already been counted, the code only `continue`s (skips) when the *exact same signature bytes* were already seen. If the address is the same but the raw signature bytes differ, the branch falls through, calls only a CPU-time guard (`MUtil.checkCPUTime()`), and then adds the address's weight to `totalWeight` again.

ECDSA signature validation used for recovery, `ECKey.ECDSASignature.validateComponents` [2](#0-1) , does not require `s` to be in the lower half of the curve order (no canonical/low-S enforcement) — only that `1 <= r,s < N` and `v ∈ {27,28}`. Consequently, for any valid signature `(r, s, v)`, the malleable variant `(r, N-s, v')` (with flipped parity) is also a valid, independently-verifiable signature that recovers to the *same* address. `recoverAddrBySign` [3](#0-2)  accepts both variants.

This is precisely the bug class described in the external report: an authorization decision is made by checking a nominally-unique identifier (here, "have we already counted this signer") using a check that is not actually unique across the relevant input space (raw signature bytes rather than the underlying identity/address), allowing a single principal to be miscounted as multiple independent principals — exactly analogous to Polkit trusting a reusable PID as a stable subject identity.

Notably, java-tron's own transaction-level multisig weight check, `TransactionCapsule.checkWeight`, was patched via a hardfork (`ForkBlockVersionEnum.VERSION_4_7_1`) to switch its dedupe key from the raw signature to `encode58Check(address)` [4](#0-3)  — i.e., the exact malleability double-counting flaw was previously fixed for on-chain transaction signature verification, but the same fix was never applied to the `ValidateMultiSign` TVM precompile, which still relies on the raw signature bytes for its "already-counted" fast-path.

### Impact Explanation
Any smart contract that implements custom multisig/threshold-authorization logic using the `ValidateMultiSign` precompile (this is its documented purpose — see the test at `ValidateMultiSignContractTest`) can be tricked into believing that N independent signers approved an operation when in fact only `ceil(N/2)` (in the worst case, as few as 1) distinct private keys actually signed, by supplying malleable signature pairs for the same key(s). This is a direct authentication-bypass primitive at the TVM level: attacker-controlled or single-key-compromised principals can satisfy multisig thresholds enforced by contract-level access control (e.g., custom multisig wallets, DAOs, or asset-custody contracts built on top of TIP-854's `ValidateMultiSign`), leading to unauthorized account operations and potential theft of custodied funds.

### Likelihood Explanation
Exploitation requires only: (1) knowledge of one private key that holds weight in the target `Permission`, and (2) the ability to derive the malleable counterpart of that key's signature (`N - s`, flipped `v`), which is a trivial, well-known, purely mathematical operation requiring no additional secrets. Any unprivileged caller who can invoke a contract using `ValidateMultiSign` (i.e., craft the `data` payload with two signature entries derived from one key) can trigger this. The barrier to exploitation is low; the only constraint is that the target's authorization logic must actually rely on `ValidateMultiSign`'s threshold result to gate a sensitive operation.

### Recommendation
Patch `ValidateMultiSign.execute` in `PrecompiledContracts.java` to deduplicate strictly by `recoveredAddr` (mirroring the fix already applied in `TransactionCapsule.checkWeight` after fork `VERSION_4_7_1`): once an address has contributed weight, any further signature recovering to that same address must be skipped entirely (not merely deduped by exact signature bytes), regardless of whether the raw bytes differ due to malleability. Additionally, consider enforcing canonical (low-S) signatures in signature validation/recovery paths used for authorization decisions to eliminate the malleable-variant class of issues at the source.

### Proof of Concept
1. Create an account with an `Active` permission requiring `threshold = 2`, with two keys `key1` (weight 1) and `key2` (weight 1) — as set up in `ValidateMultiSignContractTest.testDifferentCase` [5](#0-4) .
2. Using only `key1`, sign the hash to get `sig1 = (r, s, v)`.
3. Compute the malleable counterpart `sig1' = (r, N - s, v ^ 1)` — a valid, independently-verifiable signature for the same message and same address as `sig1`.
4. Invoke `ValidateMultiSign(address, permissionId, data, [sig1, sig1'])` via the TVM precompile.
5. In `execute`, the first iteration recovers `key1`'s address, adds weight 1, and records `executedSignList`. The second iteration recovers the same address again; because `sig1'` bytes differ from `sig1`, the inner `matrixContains(executedSignList, sign)` check fails, so the code does not `continue` — it adds weight 1 again, yielding `totalWeight = 2 >= threshold (2)`, returning `dataOne()` (success) despite only one actual private key having signed.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L371-388)
```java
  private static byte[] recoverAddrBySign(byte[] sign, byte[] hash) {
    byte[] out = null;
    if (ArrayUtils.isEmpty(sign) || sign.length < 65) {
      return new byte[0];
    }
    try {
      Rsv rsv = Rsv.fromSignature(sign);
      SignatureInterface signature = SignUtils.fromComponents(rsv.getR(), rsv.getS(), rsv.getV(),
          CommonParameter.getInstance().isECKeyCryptoEngine());
      if (signature.validateComponents()) {
        out = SignUtils.signatureToAddress(hash, signature,
            CommonParameter.getInstance().isECKeyCryptoEngine());
      }
    } catch (Throwable any) {
      logger.info("ECRecover error", any.getMessage());
    }
    return out;
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1080-1106)
```java
      AccountCapsule account = this.getDeposit().getAccount(address);
      if (account != null) {
        try {
          Permission permission = account.getPermissionById(permissionId);
          if (permission != null) {
            //calculate weight
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

**File:** crypto/src/main/java/org/tron/common/crypto/ECKey.java (L923-946)
```java
    public static boolean validateComponents(BigInteger r, BigInteger s,
        byte v) {

      if (v != 27 && v != 28) {
        return false;
      }

      if (BIUtil.isLessThan(r, BigInteger.ONE)) {
        return false;
      }
      if (BIUtil.isLessThan(s, BigInteger.ONE)) {
        return false;
      }

      if (!BIUtil.isLessThan(r, SECP256K1N)) {
        return false;
      }
      return BIUtil.isLessThan(s, SECP256K1N);
    }


    public boolean validateComponents() {
      return validateComponents(r, s, v);
    }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/TransactionCapsule.java (L243-263)
```java
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

**File:** framework/src/test/java/org/tron/common/runtime/vm/ValidateMultiSignContractTest.java (L80-100)
```java
    Protocol.Permission activePermission =
        Protocol.Permission.newBuilder()
            .setType(Protocol.Permission.PermissionType.Active)
            .setId(2)
            .setPermissionName("active")
            .setThreshold(2)
            .setOperations(ByteString.copyFrom(ByteArray
                .fromHexString("0000000000000000000000000000000000000000000000000000000000000000")))
            .addKeys(Protocol.Key.newBuilder().setAddress(ByteString.copyFrom(key1.getAddress()))
                .setWeight(1).build())
            .addKeys(
                Protocol.Key.newBuilder()
                    .setAddress(ByteString.copyFrom(key2.getAddress()))
                    .setWeight(1)
                    .build())
            .build();

    toAccount
        .updatePermissions(toAccount.getPermissionById(0), null,
            Collections.singletonList(activePermission));
    dbManager.getAccountStore().put(key.getAddress(), toAccount);
```
