### Title
ECDSA Signature Malleability in `ValidateMultiSign` TVM Precompile Allows Multisig Weight Double-Counting - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The root ECDSA signature primitive in java-tron does not enforce canonical (low-s) signatures — `ECKey.ECDSASignature.validateComponents()` only checks that `r` and `s` lie in `[1, SECP256K1N)`, not that `s` is in the lower half of the curve order. [1](#0-0) 
This means, for any private key, there exist two distinct valid byte-encodings of a signature over the same hash: `(r, s, v)` and `(r, n-s, v')`, both of which recover to the same address. This is the classic EIP-2098 / signature-malleability issue cited in the report. `ECDSASignature.toCanonicalised()` exists but is never invoked by the verification paths, so malleable variants are accepted everywhere `validateComponents()`/`signatureToAddress` are used.

### Finding Description
The `ValidateMultiSign` precompiled contract (TVM address `0x0a`, invoked from any deployed smart contract to check M-of-N multisig approval) aggregates weight per recovered signer with flawed duplicate detection: [2](#0-1) 

The loop only skips (`continue`) adding weight when the **exact same signature bytes** (`address ++ raw signature`) have already been counted (`ByteArray.matrixContains(executedSignList, sign)`). If the same signer's address has already been counted but the current signature's raw bytes differ (e.g., a malleable `(r, n-s, v')` variant of a previously supplied `(r, s, v)` signature over the identical hash `hash`), the code falls through past the `matrixContains(... , sign)` check, computes `weight = TransactionCapsule.getWeight(...)` again, and adds it to `totalWeight` a second time via `executedSignList.add(sign); executedSignList.add(recoveredAddr);`.

Because `recoverAddrBySign` relies on `signature.validateComponents()` (via `SignUtils.fromComponents`/`Rsv.fromSignature`) which does not reject non-canonical `s`, an attacker holding a single valid signature `(r, s, v)` can trivially derive the malleable counterpart `(r, n-s, v')` and submit both as two entries in the `signatures` array passed to `ValidateMultiSign`. Both recover to the identical address, and the flawed dedup logic counts that single key's weight twice (or more, since each malleable pair produces two variants).

Contrast this with the core (non-TVM) transaction-level multisig check `TransactionCapsule.checkWeight`, which was hardened post-fork (`ForkBlockVersionEnum.VERSION_4_7_1`) to dedup by **recovered address** rather than raw signature bytes: [3](#0-2) 
The `ValidateMultiSign` TVM precompile never received the equivalent address-based dedup fix, leaving the TVM-facing multisig-weight aggregation vulnerable to the same class of bug that was already patched for on-chain transaction signature verification.

### Impact Explanation
`ValidateMultiSign` (address `0x0a`) is a general-purpose precompile any deployed contract can call to implement M-of-N multisig authorization gating (e.g., custody vaults, smart wallets, DAO-style approval gates) using `AccountCapsule`/`Permission` weight data. Because the dedup logic can be bypassed via signature malleability, a single signer's approval weight can be double- (or multiply-) counted, letting an attacker satisfy a permission `threshold` that was meant to require multiple independent signers, using only one compromised or colluding key. This is a concrete unauthorized-authorization bypass that can lead to theft of funds or unauthorized account operations in any contract relying on this precompile to enforce multisig thresholds.

### Likelihood Explanation
Exploitation requires only: (1) one valid ECDSA signature `(r, s, v)` produced by a legitimate key over the relevant hash, and (2) trivial arithmetic (`s' = n - s`, flip `v` parity) to derive the second malleable form — no private key knowledge beyond having one valid signature is needed. The attacker (the transaction broadcaster calling the contract that uses this precompile) fully controls the `signatures` array passed into the precompile call, so this is reachable directly from an unprivileged, ordinary transaction/contract call.

### Recommendation
Enforce canonical low-s signatures at the point of ECDSA validation (`ECDSASignature.validateComponents()` in `ECKey.java`, and the analogous checks in `SignUtils`/`Rsv`), or fix `ValidateMultiSign`'s deduplication to key exclusively off the **recovered address** (mirroring the `VERSION_4_7_1` fix already applied in `TransactionCapsule.checkWeight`) rather than off the concatenation of address + raw signature bytes. Apply the same audit to `BatchValidateSign` and any other precompile/aggregation logic that de-duplicates by raw signature bytes instead of by recovered address.

### Proof of Concept
1. Generate `ECKey key1` and build a `Permission` (Active, threshold = 2) with two authorized keys `key1`, `key2`, each weight 1 (mirroring the setup in `ValidateMultiSignContractTest.testTip854CanonicalInputUnchanged`, [4](#0-3) ).
2. Compute `hash = sha256(address ++ permissionId ++ data)` as done inside `ValidateMultiSign.execute` ( [5](#0-4) ).
3. Sign `hash` once with `key1` to get `sig1 = (r, s, v)` via `ECKey.sign`.
4. Derive the malleable counterpart `sig1' = (r, CURVE.getN().subtract(s), v_flipped)` — a byte-distinct, independently-valid signature recovering to the same `key1` address (since `validateComponents` at [1](#0-0)  does not reject `s` in the upper half).
5. Call the `ValidateMultiSign` precompile with `signatures = [sig1, sig1']` and no `key2` signature at all.
6. Trace through `doExecute`/`execute` at [6](#0-5) : iteration 1 recovers `key1`'s address, adds weight 1; iteration 2 recovers the same address again, `matrixContains(executedSignList, recoveredAddr)` is true, but `matrixContains(executedSignList, sign)` is false (different raw bytes), so it does **not** `continue` — it adds weight 1 again. `totalWeight` reaches 2 ≥ threshold 2, and the precompile returns `dataOne()` (success) despite only one of the two required keys having actually signed.

### Citations

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1062-1064)
```java
      byte[] combine = ByteUtil.merge(address, ByteArray.fromInt(permissionId), data);
      byte[] hash = Sha256Hash.hash(CommonParameter
          .getInstance().isECKeyCryptoEngine(), combine);
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1080-1108)
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

**File:** framework/src/test/java/org/tron/common/runtime/vm/ValidateMultiSignContractTest.java (L194-228)
```java
  // TIP-854 Compatibility: for canonically-shaped calldata (real 65-byte sigs,
  // total length == 5*32 + 5*32*N), behaviour must be identical pre- vs
  // post-activation — the guard is a no-op for well-formed inputs.
  @Test
  public void testTip854CanonicalInputUnchanged() {
    ECKey key = new ECKey();
    AccountCapsule toAccount = new AccountCapsule(ByteString.copyFrom(key.getAddress()),
        Protocol.AccountType.Normal,
        System.currentTimeMillis(), true, dbManager.getDynamicPropertiesStore());
    ECKey key1 = new ECKey();
    ECKey key2 = new ECKey();
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
            .addKeys(Protocol.Key.newBuilder().setAddress(ByteString.copyFrom(key2.getAddress()))
                .setWeight(1).build())
            .build();
    toAccount.updatePermissions(toAccount.getPermissionById(0), null,
        Collections.singletonList(activePermission));
    dbManager.getAccountStore().put(key.getAddress(), toAccount);

    byte[] data = Sha256Hash.hash(CommonParameter.getInstance().isECKeyCryptoEngine(), longData);
    byte[] merged = ByteUtil.merge(key.getAddress(), ByteArray.fromInt(2), data);
    byte[] toSign = Sha256Hash.hash(CommonParameter.getInstance().isECKeyCryptoEngine(), merged);
    List<Object> signs = new ArrayList<>();
    signs.add(Hex.toHexString(key1.sign(toSign).toByteArray()));
    signs.add(Hex.toHexString(key2.sign(toSign).toByteArray()));

```
