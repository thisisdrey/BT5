### Title
Multi-signature weight double-counting via ECDSA signature malleability bypasses N-of-M threshold in TVM `ValidateMultiSign` precompile - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
The `ValidateMultiSign` TVM precompile is designed to let smart contracts verify that a set of signatures collectively meets an account permission's weight threshold — the on-chain analog of a "multi-factor"/"multi-signer" authorization check. Its de-duplication logic only rejects an exact byte-for-byte repeated signature, not a second, malleable-but-valid signature from the same key. This lets a caller in possession of a single private key satisfy a multi-key threshold that is supposed to require independent signers, which is conceptually the same bypass class as CVE-2021-32800 (a single factor being accepted where multiple independent factors are required).

### Finding Description
`ValidateMultiSign.execute` iterates over the supplied signatures and accumulates `totalWeight` from the `Permission` keyed on `recoveredAddr`: [1](#0-0) 

For each signature, it computes `recoveredAddr = recoverAddrBySign(sign, hash)` and builds `sign = merge(recoveredAddr, sign)`. The dedup check is:
```
if (ByteArray.matrixContains(executedSignList, recoveredAddr)) {
  if (ByteArray.matrixContains(executedSignList, sign)) {
    continue;   // exact duplicate signature -> skip
  }
  MUtil.checkCPUTime();   // different signature, same address -> NOT skipped
}
long weight = TransactionCapsule.getWeight(permission, recoveredAddr);
...
totalWeight += weight;
executedSignList.add(sign);
executedSignList.add(recoveredAddr);
```
If the *same address* has already contributed weight, the code only skips accumulation when the raw signature bytes are byte-identical to a previous one. If an attacker supplies a second, distinct-but-valid signature for the same address (recoverable via standard ECDSA signature malleability — flipping `s` to `n-s` together with the matching recovery id `v` yields a different 65-byte `r||s||v` signature that still recovers to the same address for the same hash), the `matrixContains(executedSignList, sign)` check fails (bytes differ), execution falls through the `MUtil.checkCPUTime()` call (only an anti-DoS timing guard, not a rejection), and `weight` is added to `totalWeight` a second time for the very same key.

This differs from the account-level signature checker `TransactionCapsule.checkWeight` (`chainbase/src/main/java/org/tron/core/capsule/TransactionCapsule.java:233-270`), which explicitly throws `PermissionException("... has signed twice!")` when the same signer address appears more than once [2](#0-1) . The TVM precompile lacks this hard rejection and instead silently double-counts.

### Impact Explanation
Any TVM contract that relies on `ValidateMultiSign` (the `validatemultisign(address,uint256,bytes32,bytes[])` precompile, directly callable by any deployed contract in response to an arbitrary `TriggerSmartContract` transaction from any unprivileged account) to gate a privileged action — e.g., a custom multisig wallet or approval-gated fund-release contract that requires K independent keys before releasing TRC-10/TRC-20 tokens or TRX — can be tricked into believing that K independent signers approved an operation when in fact only a single private key produced two malleable signatures. This is an authorization/authentication bypass that can lead to unauthorized fund transfers from contracts built on top of this precompile, matching the "concrete unauthorized account operation / theft of funds" bar.

### Likelihood Explanation
Exploitation only requires knowledge of one private key that already holds partial weight in the target `Permission`, plus the ability to compute the standard ECDSA malleable counterpart of a signature (`s' = n - s`, flipped `v`) — a trivial, well-known operation requiring no special access. The call path (any contract calling the precompile, reachable via a normal signed `TriggerSmartContract`) is available to any anonymous transaction broadcaster, so likelihood is high wherever a contract's business logic actually depends on `ValidateMultiSign`'s threshold result for authorization decisions.

### Recommendation
Change the dedup logic in `ValidateMultiSign.execute` to key strictly on `recoveredAddr` (not on the raw `sign` bytes): if the address has already contributed weight, always `continue` (skip) regardless of whether the signature bytes match exactly, mirroring the address-keyed rejection already used in `TransactionCapsule.checkWeight`. Alternatively, normalize/canonicalize signatures (reject non-canonical `s` values, as is standard EIP-2 style enforcement) before address recovery so malleable duplicates cannot be constructed in the first place.

### Proof of Concept
1. Create an account with an `Active` permission requiring `threshold = 2`, containing key `K1` with weight 1 and key `K2` with weight 1 (2-of-2 style multisig), as in the existing test setup [3](#0-2) .
2. Using only `K1`'s private key, sign the precompile's message hash to get signature `sig1 = (r, s, v)`.
3. Compute the malleable counterpart `sig1' = (r, n - s, v ⊕ 1)`, which recovers to the same address as `K1` for the same hash.
4. Call `validatemultisign(address, permissionId, dataHash, [sig1, sig1'])`.
5. In `ValidateMultiSign.execute`, the first iteration adds `K1`'s weight (1) to `totalWeight`. In the second iteration, `recoveredAddr` (== `K1`) is found in `executedSignList`, but `sign` (the merged bytes of `sig1'`) is not identical to the previously stored merged `sig1`, so the inner `continue` does not trigger; weight is added a second time, making `totalWeight = 2 >= threshold`, and the precompile returns `true` (`dataOne()`) even though only one private key (`K1`) ever signed — `K2` never participated.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1088-1106)
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
                //incorrect sign
                return Pair.of(true, DATA_FALSE);
              }
              totalWeight += weight;
              executedSignList.add(sign);
              executedSignList.add(recoveredAddr);
            }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/TransactionCapsule.java (L257-263)
```java
      if (ForkController.instance().pass(Parameter.ForkBlockVersionEnum.VERSION_4_7_1)) {
        base64 = encode58Check(address);
      }
      if (addMap.containsKey(base64)) {
        throw new PermissionException(encode58Check(address) + " has signed twice!");
      }
      addMap.put(base64, weight);
```

**File:** framework/src/test/java/org/tron/common/runtime/vm/ValidateMultiSignContractTest.java (L80-99)
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
```
