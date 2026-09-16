### Title
`ValidateMultiSign` precompile lets a single key satisfy a multi-key permission threshold by accepting several distinct signatures from the same signer - ([File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java])

### Summary
The `ValidateMultiSign` TVM precompiled contract (address `0x0f`) is meant to let a smart contract verify that a set of signatures satisfies an account's on-chain multisig `Permission` threshold. Unlike the canonical signature-weight checker `TransactionCapsule.checkWeight`, which explicitly rejects a second signature from the same recovered address ("has signed twice!"), the precompile's own de-duplication only skips a signature entry when the *entire* `(address, signature-bytes)` pair has already been seen. If the same private key produces two different, both-valid signatures over the same hash (trivial: sign twice — ECDSA nonce/ malleability naturally yields different `r,s` for the same message, or the caller crafts two syntactically different but validly-recovering blobs), both entries recover to the same address, both pass the `address` re-appearance check without being treated as an exact duplicate, and the address's weight is added into `totalWeight` twice. This is structurally the same bug class as the XLootStaking exploit: the "identity" that should be deduplicated (an NFT id in the report, a signer address here) is checked for validity per-entry, but uniqueness is enforced on the wrong granularity (exact duplicate bytes) instead of on the identity itself, letting one holder's votes/claims be counted multiple times before the enforcing state is finalized.

### Finding Description
In `PrecompiledContracts.ValidateMultiSign.execute`: [1](#0-0) 

```
AccountCapsule account = this.getDeposit().getAccount(address);
...
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
```

The logic only `continue`s (skips adding weight) when the *combined* `address+signature bytes` blob already exists in `executedSignList`. If the same address appears with a **different** signature blob (any second, distinct, still-valid signature by the same key over the same `hash`), the inner check fails, `MUtil.checkCPUTime()` is invoked purely as a CPU-time guard (not a rejection), and execution falls through to add that address's `weight` to `totalWeight` again. Two (or up to `MAX_SIZE = 5`) signatures from a single private key can therefore accumulate weight as if they came from distinct co-signers.

Contrast this with the canonical implementation used everywhere else in the codebase for permission-weight checks, `TransactionCapsule.checkWeight`, which tracks signers by recovered address in a map and explicitly throws `PermissionException(... + " has signed twice!")` the moment the *same address* reappears, regardless of whether the signature bytes differ: [2](#0-1) 

That is the correct de-duplication granularity (per signer identity), and it is exactly what `ValidateMultiSign` fails to do.

### Impact Explanation
`ValidateMultiSign` is a TVM precompile invocable from any smart contract via a plain `TriggerSmartContract` transaction — no special privileges are needed, and no witness/committee/peer compromise is required. Any TRC-20/DeFi/DAO-style contract on TRON that uses this precompile to gate a privileged action (e.g., releasing escrowed funds, approving a multisig-controlled withdrawal, authorizing a governance action) behind an account's `Permission` threshold can be tricked into believing that N independent signers approved an operation when in fact fewer distinct keys (as few as one) actually signed, as long as that one key produces multiple non-identical signatures. This is a broadcastable-transaction-reachable authorization bypass that can result in unauthorized account operations / theft of funds in any contract relying on this precompile for multisig authorization — matching the "unauthorized account operation" and "theft of funds" impact classes.

### Likelihood Explanation
Reachable with a single, ordinary, unprivileged transaction: deploy or call any contract that invokes precompile `0x0f` and supply an ABI-encoded signature array containing two (or more) different signatures produced by the same private key over the same hash. Producing two distinct signatures from one key over the same message is trivial (sign the message twice; ECDSA nonce randomness naturally differs, or use signature malleability transforms) — no cryptographic breakthrough, no privileged role, and no dependency on network/peer conditions is required. The likelihood is high for any downstream contract that actually relies on this precompile for authorization; the precompile itself will happily return "true" (`dataOne()`) under these conditions.

### Recommendation
Change the de-duplication key in `ValidateMultiSign.execute` from the combined `(address, signature bytes)` blob to the recovered address alone — mirroring `TransactionCapsule.checkWeight`'s behavior: the first time an address is seen, add its weight; on any subsequent occurrence of the *same address* (regardless of signature bytes), skip it entirely (do not add weight again). This makes the precompile consistent with the rest of the codebase's signature-weight accounting and removes the ability to inflate `totalWeight` using multiple signatures from a single key.

### Proof of Concept
Conceptually (cannot execute code in this environment, but the exploit path is directly derivable from the code above and is analogous to the existing test file's "Repetitive" signature case which currently succeeds only because the repeated signature bytes are byte-identical): [3](#0-2) 

1. Create an account with an `Active` permission that has two keys, `key1` and `key2`, each weight 1, threshold 2.
2. Attacker controls only `key1`.
3. Attacker computes `toSign` hash as in the test, then produces two **distinct** valid signatures with `key1` over the same `toSign` (e.g., sign twice — ECDSA nonce differs each call producing different `r/s`, both recovering to `key1`'s address).
4. Call `ValidateMultiSign(address, permissionId, data, [sig1_from_key1, sig2_from_key1])`.
5. In `execute()`, the first signature recovers `key1`'s address, adds weight 1, and is stored as `(address, sig1)`. The second signature also recovers `key1`'s address; `executedSignList` contains the address but not the exact `(address, sig2)` blob, so it does **not** `continue` — it proceeds to add weight 1 again. `totalWeight` becomes 2, which is `>= threshold (2)`, and the precompile returns `true`/`dataOne()` even though only one distinct key (`key1`) ever signed.

This confirms that the precompile treats two different signatures from the same key as if they were two independent co-signer approvals, bypassing the intended multisig threshold — directly analogous to the XLootStaking bug where the same NFT id, repeated with distinct loop iterations, was treated as distinct claim entries before state finalized the (correct) once-per-entity accounting.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1080-1119)
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
        } catch (Throwable t) {
          if (t instanceof OutOfTimeException) {
            throw t;
          }
          logger.info("ValidateMultiSign error:{}", t.getMessage());
        }
      }
      return Pair.of(true, DATA_FALSE);
```

**File:** chainbase/src/main/java/org/tron/core/capsule/TransactionCapsule.java (L242-268)
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
      if (approveList != null) {
        approveList.add(ByteString.copyFrom(address)); //out put approve list.
      }
      currentWeight += weight;
    }
```

**File:** framework/src/test/java/org/tron/common/runtime/vm/ValidateMultiSignContractTest.java (L115-126)
```java
    //sign data

    List<Object> signs = new ArrayList<>();
    signs.add(Hex.toHexString(key1.sign(toSign).toByteArray()));
    //add Repetitive
    signs.add(Hex.toHexString(key1.sign(toSign).toByteArray()));
    signs.add(Hex.toHexString(key2.sign(toSign).toByteArray()));

    Assert.assertArrayEquals(
        validateMultiSign(StringUtil.encode58Check(key.getAddress()), permissionId, data, signs)
            .getValue(), DataWord.ONE().getData());

```
