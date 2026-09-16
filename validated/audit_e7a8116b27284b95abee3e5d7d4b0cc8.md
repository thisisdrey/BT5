### Title
Duplicate-signer weight double-counting in `ValidateMultiSign` precompile bypasses multisig threshold - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The `ValidateMultiSign` TVM precompiled contract, used by smart contracts to verify TRON account multisig approvals on-chain, deduplicates signatures by comparing the raw signature bytes rather than by the recovered signer address. Because ECDSA signing is non-deterministic, a single private key can produce multiple distinct, valid signatures over the same message hash. The precompile's dedup check only skips a signature when the *exact byte sequence* was already counted for that signer; a second, differently-encoded valid signature from the same key is not skipped and its weight is added again, letting one signer's weight be counted more than once and satisfy the permission threshold alone.

### Finding Description
`ValidateMultiSign.execute` iterates over the supplied signature array and, for each signature, recovers the signer address and accumulates weight: [1](#0-0) 

The loop tracks previously seen signer addresses/signatures in `executedSignList` and only `continue`s (skips) when **both** the recovered address *and* the exact merged `(recoveredAddr, sign)` byte sequence have already been recorded:

```java
if (ByteArray.matrixContains(executedSignList, recoveredAddr)) {
  if (ByteArray.matrixContains(executedSignList, sign)) {
    continue;
  }
  MUtil.checkCPUTime();
}
long weight = TransactionCapsule.getWeight(permission, recoveredAddr);
...
totalWeight += weight;
```

`matrixContains` performs a byte-for-byte `Arrays.equals` comparison, not an address-only comparison: [2](#0-1) 

If the same private key signs the same hash twice, ECDSA (unless deterministically nonce-derived) yields two different valid `(r, s)` pairs, hence two different signature byte strings that both recover to the same address. The first occurrence is recorded; on the second occurrence, `recoveredAddr` is already present in `executedSignList`, but the raw `sign` bytes are not, so the inner `continue` does not trigger — execution falls through, calls `MUtil.checkCPUTime()` (a CPU/DoS guard, not a security check), and then adds `weight` for that signer **again** into `totalWeight`.

This is the same bug class as the external report: the check enforces signature *count* / recoverability, but not signer *uniqueness*, allowing an attacker who controls a single key with weight below threshold to reach `totalWeight >= permission.getThreshold()` by supplying two distinct valid signatures from that one key.

By contrast, the transaction-level multisig verification path in `TransactionCapsule.checkWeight` (used for signing/broadcasting `Transaction`s) correctly deduplicates by address, not by raw signature bytes, and throws `"has signed twice!"` if the same signer appears twice: [3](#0-2) 

This confirms the correct pattern exists elsewhere in the codebase but was not applied consistently to the `ValidateMultiSign` precompile.

### Impact Explanation
`ValidateMultiSign` is a general-purpose precompiled contract callable by any smart contract logic (multisig wallets, escrow, custody, governance contracts built on TVM that delegate M-of-N approval checks to this precompile instead of re-implementing signature aggregation). Any contract relying on this precompile to gate a privileged action (fund release, withdrawal approval, ownership change) can be tricked into believing an M-of-N threshold was met when only a single signer actually approved, as long as that signer's weight is less than the threshold but the attacker can supply two distinct valid signatures from that one key. This is a broken access-control/authorization primitive reachable from any contract call, and can lead directly to unauthorized execution of privileged operations and theft of funds in contracts depending on this precompile for multisig authorization.

### Likelihood Explanation
The precondition is only that the attacker possesses one signing key associated with the account/permission being validated (a normal case for any legitimate co-signer with partial weight) and can produce two distinct valid ECDSA signatures over the same hash — trivial since standard ECDSA signing with a random nonce `k` is non-deterministic, and the attacker fully controls their own signing process. No cooperation from other signers, no privileged network role, and no race condition is required; a single crafted call to the precompile with a hand-built signatures array is sufficient. This is reachable by any account able to deploy/call a contract that invokes the precompile.

### Recommendation
Change the deduplication logic in `ValidateMultiSign.execute` to key exclusively on the recovered signer address (as `TransactionCapsule.checkWeight` already does), rejecting or skipping weight accumulation for any repeated address regardless of the specific signature bytes presented, e.g. maintain a set of already-counted `recoveredAddr` values and skip/`continue` whenever an address is seen a second time, independent of whether the exact signature bytes differ.

### Proof of Concept
1. Set up an account with an `Active` permission of `threshold = 2` containing key `K1` (weight 1) and key `K2` (weight 1), matching the test scaffolding in `ValidateMultiSignContractTest.testDifferentCase` ( [4](#0-3) ).
2. Attacker controls only `K1` (weight 1, below threshold 2).
3. Attacker signs the target hash with `K1` twice using independent random nonces `k1 ≠ k2` (or by supplying two syntactically distinct but valid encodings recovering to `K1`), producing signatures `sig1 ≠ sig2` as raw bytes but both recovering to `K1`'s address.
4. Call the `ValidateMultiSign` precompile with `signatures = [sig1, sig2]`.
5. In the loop: for `sig1`, `recoveredAddr` (K1) is new — `weight=1` added, `totalWeight=1`. For `sig2`, `recoveredAddr` (K1) is already in `executedSignList`, but `merge(recoveredAddr, sig2)` is not (different signature bytes) — the inner `continue` is skipped, `weight=1` is added again, `totalWeight=2`.
6. `totalWeight (2) >= permission.getThreshold() (2)` is satisfied, and the precompile returns `Pair.of(true, dataOne())` (success), even though only one distinct signer (`K1`) actually signed.

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

**File:** common/src/main/java/org/tron/common/utils/ByteArray.java (L189-196)
```java
  public static boolean matrixContains(List<byte[]> source, byte[] obj) {
    for (byte[] sobj : source) {
      if (Arrays.equals(sobj, obj)) {
        return true;
      }
    }
    return false;
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

**File:** framework/src/test/java/org/tron/common/runtime/vm/ValidateMultiSignContractTest.java (L68-99)
```java
  @Test
  public void testDifferentCase() {
    //Create an account with permission

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
            .addKeys(
                Protocol.Key.newBuilder()
                    .setAddress(ByteString.copyFrom(key2.getAddress()))
                    .setWeight(1)
                    .build())
            .build();

    toAccount
        .updatePermissions(toAccount.getPermissionById(0), null,
            Collections.singletonList(activePermission));
```
