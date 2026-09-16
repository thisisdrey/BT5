## Title
Multi-signature threshold bypass via ECDSA signature malleability in `ValidateMultiSign` precompile - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

## Summary
The `ValidateMultiSign` TVM precompiled contract deduplicates submitted signatures by comparing raw signature *bytes* rather than the cryptographically recovered signer *address*. Because ECDSA signatures are malleable (for any valid `(r, s, v)` there exists a second, distinct, equally-valid encoding `(r, n-s, 1-v)` that recovers to the same address), an attacker holding a single private key can submit two different byte-level signature encodings that both recover to the same key, and have that key's weight counted twice. This lets a caller satisfy a multi-key/multi-weight threshold using fewer distinct private keys than the permission actually requires — an authentication bypass conceptually identical to the Grafana bug where a non-unique identity attribute (email) was trusted as if it uniquely identified an account. Here, the non-unique attribute is "the exact signature byte string," used as a stand-in for "the signer's identity," when the identity actually deduplicated should be the recovered address.

## Finding Description
`PrecompiledContracts.ValidateMultiSign.execute()` iterates over the caller-supplied signature array and accumulates weight for each recovered signer: [1](#0-0) 

For each signature, it recovers the signer address (`recoverAddrBySign`), builds a compound key `sign = merge(recoveredAddr, sign)`, and only *skips* adding weight if that exact compound key (address **and** original signature bytes) was already seen:

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

`ByteArray.matrixContains` performs exact byte-array equality: [2](#0-1) 

If the *address* was already seen but the newly supplied signature bytes differ from the previously seen ones (which happens trivially with ECDSA malleability, or even ASN.1/legacy re-encodings that recover to the same key), the inner `matrixContains(executedSignList, sign)` check is false, so execution does **not** `continue`; instead it proceeds to add that key's `weight` to `totalWeight` a second time. The only real cryptographic gate applied elsewhere, `SignatureInterface.validateComponents()` (used inside `recoverAddrBySign`), only rejects out-of-range `r`/`s`/`v` values — it does not reject the canonical malleable counterpart `(r, n-s, flipped v)`, which is a perfectly valid signature over the curve order: [3](#0-2) 

This differs from the transaction-level multisig path (`TransactionCapsule.checkWeight`), which — at least after fork `VERSION_4_7_1` — deduplicates using the *recovered address* (`encode58Check(address)`), not the raw signature bytes: [4](#0-3) 

The `ValidateMultiSign` precompile has no equivalent unconditional address-based dedupe — its logic only blocks the exact duplicate byte string, not other valid encodings of the same signer.

## Impact Explanation
Any smart contract that relies on `ValidateMultiSign` (address `0x100` on TVM) to gate high-value operations (custody contracts, multisig wallets, escrow, bridges, DAOs) based on an N-of-M key threshold can be bypassed by an attacker who controls fewer than N of the real keys, by supplying malleable duplicate signatures for the keys they do control. This is a concrete authentication bypass leading to unauthorized execution of privileged multisig-gated operations (e.g., unauthorized fund transfers or authorization decisions), matching the "unauthorized account operation / theft of funds" impact bar.

## Likelihood Explanation
Exploitation requires only:
1. Control of at least one key that is part of the permission's key set.
2. Producing the standard ECDSA malleable counterpart of a valid signature (trivial: flip `s -> n-s`, `v -> 1-v`), which is a well-known, deterministic transformation requiring no additional cryptographic material.
3. Calling the `ValidateMultiSign` precompile (directly reachable from any TVM contract/attacker-deployed contract) with the two byte-distinct signatures for the same key, alongside signatures for the remaining threshold weight.

No special privileges are needed beyond deploying/calling a contract, making this reachable by any unprivileged contract deployer/caller.

## Recommendation
In `PrecompiledContracts.ValidateMultiSign.execute()`, deduplicate strictly by the recovered address (as `TransactionCapsule.checkWeight` does), and reject/skip counting weight the moment an address repeats, regardless of whether the raw signature bytes differ:

```java
if (ByteArray.matrixContains(executedAddrList, recoveredAddr)) {
    continue; // do not accumulate weight for an already-counted signer
}
...
executedAddrList.add(recoveredAddr);
```

Additionally, enforce canonical signature form (reject non-canonical `s` values, e.g. `s > n/2`) before recovery, consistent with `ECDSASignature.toCanonicalised()` used elsewhere, to eliminate malleable duplicates at the source.

## Proof of Concept
1. Create an account with an `Active` permission requiring threshold `2`, with two keys `A` (weight 1) and `B` (weight 1) — attacker controls only key `A`'s private key.
2. Attacker signs the multisig hash with key `A`, producing signature `sigA = (r, s, v)`.
3. Attacker derives the malleable counterpart `sigA' = (r, n-s, 1-v)` (same recovered address `A`).
4. Attacker calls the `ValidateMultiSign` precompile with `signatures = [sigA, sigA']` for the account/permission.
5. In `execute()`: first iteration recovers `A`, not yet in `executedSignList` → adds weight 1 (`totalWeight = 1`). Second iteration recovers `A` again; `executedSignList` contains address `A` but not the exact bytes of `sigA'` → does not `continue`; instead weight 1 is added again (`totalWeight = 2`).
6. `totalWeight (2) >= permission.getThreshold() (2)` → precompile returns `true`/`ONE`, i.e., the threshold check passes using only key `A`, bypassing the requirement to also present key `B`'s signature. [5](#0-4)

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

**File:** crypto/src/main/java/org/tron/common/crypto/ECKey.java (L923-941)
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
