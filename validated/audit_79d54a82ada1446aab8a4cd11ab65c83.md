### Title
Duplicate-signature double-counting toward multisig threshold in `ValidateMultiSign` precompile - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
The `ValidateMultiSign` TVM precompiled contract (`0x0e` on java-tron, reachable from any deployed smart contract via a normal `TriggerSmartContract` call) computes a signer's approval weight against an account's multisig `Permission` threshold. Its de-duplication logic only skips a signature when the *exact same signature bytes* were already seen for a given recovered address; it does not de-duplicate by recovered address (key) alone. Because ECDSA signatures are non-unique for a fixed (key, message) pair (an attacker holding one private key can trivially produce multiple distinct valid signatures over the same hash, e.g. via signature malleability or a different nonce), a single key's weight can be counted more than once toward `permission.getThreshold()`. This mirrors the CWE-347 pattern from the referenced TUF advisory: multiple signatures resolving to the same authorized key are each counted separately toward the threshold instead of once.

### Finding Description
`TransactionCapsule.checkWeight` (used for on-chain transaction signature verification in `chainbase/src/main/java/org/tron/core/capsule/TransactionCapsule.java:233-270`) correctly de-duplicates by the recovered **address**, throwing `PermissionException` if the same signer address appears twice — this is the "correct" pattern, active on current mainnet since `ForkBlockVersionEnum.VERSION_4_7_1` (block version 27) is far below the current `BLOCK_VERSION` (37) and its `hardForkTime` (2020-08-07) has long passed.

However, `PrecompiledContracts.ValidateMultiSign.execute` (`actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java:1036-1120`) implements its own, weaker weight-accumulation loop:

```java
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
```
(actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java:1088-1106)

The only "skip" (`continue`) path fires when both the recovered address **and** the exact raw signature bytes have already been seen (an identical, byte-for-byte duplicate signature). If the recovered address has been seen before but the raw signature bytes differ — which is trivially achievable for the same private key (ECDSA signature malleability: flipping `s → n-s` and the recovery id yields a second valid signature for the same hash and same key; or simply re-signing with a different nonce) — the code falls through to `MUtil.checkCPUTime()` and then unconditionally adds `weight` to `totalWeight` again. There is no check that the address hasn't already contributed weight.

This lets a caller holding one key with weight `W` submit two (or more) distinct valid signatures from that same key to accumulate `2W, 3W, ...` toward `permission.getThreshold()`, even though only one authorized key actually approved the operation.

### Impact Explanation
`ValidateMultiSign` is used by TVM smart contracts to gate privileged operations behind an account's multisig permission (e.g., escrow/vault contracts, DAO-style multisig wallets, custodial contracts that require N-of-M approval before releasing funds or performing an administrative action). Any contract relying on this precompile to enforce a real multisig threshold can be bypassed by an attacker who controls just one of the required keys (with weight below the threshold), by supplying multiple distinct signatures derived from that single key. This allows an unauthorized single-key holder to satisfy a multi-key threshold check, resulting in unauthorized account operations / theft of funds gated by that permission from any TVM contract that uses `ValidateMultiSign` for authorization.

### Likelihood Explanation
Reachable by any address that can call a smart contract invoking the precompile (any contract deployer/caller — no special privilege needed). Generating a second valid ECDSA signature for the same message/key is computationally trivial (standard signature malleability: `(r, s, v) → (r, n-s, 1-v)`, or simply re-signing with a fresh nonce). No SR/witness/peer collusion or protocol-level privilege is required — a single externally-owned account with knowledge of one signing key of a target multisig permission can construct the exploit entirely off-chain and submit a normal `TriggerSmartContract` transaction.

### Recommendation
De-duplicate `ValidateMultiSign`'s weight accumulation strictly by recovered address (mirroring `TransactionCapsule.checkWeight`'s address-keyed `HashMap`/`PermissionException` approach), rejecting or skipping any signature whose recovered address has already contributed weight, regardless of whether the raw signature bytes match.

### Proof of Concept
1. Deploy/reference an account with an `Active` permission requiring `threshold = 2`, containing key `K1` (weight 1) and key `K2` (weight 1).
2. Attacker controls only `K1`.
3. Attacker computes `sigA = sign(K1, hash)` normally, and `sigB` = the malleable counterpart of `sigA` (flip `s` to `n - s`, adjust recovery id) — both `sigA` and `sigB` are valid signatures recovering to `K1`'s address but are byte-different.
4. Attacker calls a contract that invokes `ValidateMultiSign(address, permissionId, data, [sigA, sigB])`.
5. In the loop: for `sigA`, `recoveredAddr = K1`, not in `executedSignList` yet → `totalWeight += 1`. For `sigB`, `recoveredAddr = K1` is now in `executedSignList`, but `merge(K1, sigB)` (exact bytes) is not → falls through → `totalWeight += 1` again.
6. `totalWeight == 2 >= threshold(2)` → precompile returns `dataOne()` (success), even though only key `K1` actually approved the action. [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1080-1111)
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

            if (totalWeight >= permission.getThreshold()) {
              return Pair.of(true, dataOne());
            }
          }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/TransactionCapsule.java (L233-270)
```java
  public static long checkWeight(Permission permission, List<ByteString> sigs, byte[] hash,
      List<ByteString> approveList)
      throws SignatureException, PermissionException, SignatureFormatException {
    long currentWeight = 0;
    if (sigs.size() > permission.getKeysCount()) {
      throw new PermissionException(
          "Signature count is " + (sigs.size()) + " more than key counts of permission : "
              + permission.getKeysCount());
    }
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
      if (approveList != null) {
        approveList.add(ByteString.copyFrom(address)); //out put approve list.
      }
      currentWeight += weight;
    }
    return currentWeight;
  }
```

**File:** common/src/main/java/org/tron/core/config/Parameter.java (L7-34)
```java
  public enum ForkBlockVersionEnum {
    ENERGY_LIMIT(5, 0L, 0),
    VERSION_3_2_2(6, 0L, 0),
    VERSION_3_5(7, 0L, 0),
    VERSION_3_6(8, 0L, 0),
    VERSION_3_6_5(9, 0L, 0),
    VERSION_3_6_6(10, 0L, 0),
    VERSION_4_0(16, 0L, 0),
    VERSION_4_0_1(17, 1596780000000L, 80),//GMT 2020-08-07 06:00:00,80 means 22 SR upgrade
    VERSION_4_1(19, 1596780000000L, 80),//GMT 2020-08-07 06:00:00,80 means 22 SR upgrade
    VERSION_4_1_2(20, 1596780000000L, 80),
    VERSION_4_2(21, 1596780000000L, 80),
    VERSION_4_3(22, 1596780000000L, 80),
    VERSION_4_4(23, 1596780000000L, 80),
    VERSION_4_5(24, 1596780000000L, 80),
    VERSION_4_6(25, 1596780000000L, 80),
    VERSION_4_7(26, 1596780000000L, 80),
    VERSION_4_7_1(27, 1596780000000L, 80),
    VERSION_4_7_2(28, 1596780000000L, 80),
    VERSION_4_7_4(29, 1596780000000L, 80),
    VERSION_4_7_5(30, 1596780000000L, 80),
    VERSION_4_7_7(31, 1596780000000L, 80),
    VERSION_4_8_0(32, 1596780000000L, 80),
    VERSION_4_8_0_1(33, 1596780000000L, 70),
    VERSION_4_8_1(34, 1596780000000L, 80),
    VERSION_4_8_1_1(35, 1596780000000L, 70),
    VERSION_4_8_2(36, 1596780000000L, 80),
    VERSION_4_8_2_2(37, 1596780000000L, 70);
```
