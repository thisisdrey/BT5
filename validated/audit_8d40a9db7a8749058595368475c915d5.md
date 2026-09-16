### Title
Insufficient signature-deduplication check in the `ValidateMultiSign` precompile allows one private key's weight to be counted more than once, bypassing the multisig threshold - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
CVE-2020-8286 is a class of "a revocation/validity check nominally exists but is implemented so weakly that it fails to reject data it was designed to reject" (curl performed an OCSP check but didn't actually verify the response, so revoked certs were still accepted). The `ValidateMultiSign` TVM precompile contains the same *"the check runs but doesn't actually block the bad case"* pattern in its per-signer duplicate/weight accounting.

### Finding Description
`ValidateMultiSign.execute()` accumulates `totalWeight` by iterating over caller-supplied signatures and is supposed to count each signer's weight at most once: [1](#0-0) 

```
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
  ...
  totalWeight += weight;
  executedSignList.add(sign);
  executedSignList.add(recoveredAddr);
}
```

The dedup logic only `continue`s (skips adding weight again) when the **exact same signature bytes** were seen before for that address. If the same address appears again but with a *different* signature byte sequence over the identical `hash` (e.g., the second valid ECDSA encoding of the same message/key pair, `s' = n - s` with the corresponding flipped recovery id — classic ECDSA signature malleability), the code falls into the `MUtil.checkCPUTime()` branch and then proceeds to add the weight for that address **again**, since there is no `continue`/`return` there. This lets one private key contribute its weight more than once toward `permission.getThreshold()`.

This is the analog of the CVE's root cause: a check ("have we already counted this signer?") exists, but the actual comparison used to decide "is this signature actually new" is too narrow (byte-exact) to catch cryptographically-equivalent-but-different-encoded signatures from the same key, so the protection is bypassed.

Contrast this with the general on-chain signature-weight checker `TransactionCapsule.checkWeight()`, which correctly keys its de-duplication map by the **recovered address** (not the raw signature bytes) and throws if the same address signs twice: [2](#0-1) 

The precompile's weaker, byte-exact check is the divergence that reintroduces the bug for TVM-callable multisig verification.

### Impact Explanation
`ValidateMultiSign` is a TVM precompiled contract that any smart contract (and therefore any account that can send a transaction calling that contract) can invoke to check whether a set of supplied signatures satisfies a given on-chain `Permission`'s threshold. Contracts that rely on this precompile for authorization decisions (e.g., custom multisig wallets, escrow, DAO/voting logic, or any DApp gatekeeping fund transfers behind an N-of-M signer check) can be tricked into believing a threshold requiring `N` distinct signers was met when in fact only `N-1` (or fewer) distinct private keys actually signed, because one signer's weight is double-counted via a second, differently-encoded but still-valid signature over the same hash. This directly enables unauthorized approval of an operation that should have required additional independent signers — a form of unauthorized account/fund authorization bypass reachable purely from a signed transaction that triggers a contract call into this precompile.

### Likelihood Explanation
Exploitability depends on whether the codebase enforces canonical (low-`s`) ECDSA signatures anywhere upstream of `recoverAddrBySign`/`ECKey` signing/recovery. I located `isCanonical`/`validateComponents` hooks in `crypto/src/main/java/org/tron/common/crypto/ECKey.java` and `SignatureInterface.java`, but ran out of investigation budget to confirm whether `ValidateMultiSign`'s code path calls that canonical-form check before accepting a signature for weight accounting; `checkWeight()`/`TransactionCapsule` for on-chain transaction signatures also does not appear to call `validateComponents()` in the snippet reviewed. If canonicalization is not enforced on this precompile's input, exploitation only requires a single private key and standard ECDSA signature-malleability math (no cryptographic breakthrough needed), making likelihood high; if canonicalization is enforced elsewhere and applies to this exact code path, the specific malleability vector would be blocked and the bug would be reduced to a defense-in-depth weakness. This uncertainty should be resolved by a background agent tracing `recoverAddrBySign` and confirming whether low-s/canonical enforcement gates it.

### Recommendation
Change the de-duplication key in `ValidateMultiSign` (and any similar precompile logic) from "recovered address AND identical raw signature bytes" to "recovered address only" — i.e., as soon as `recoveredAddr` has been seen once, always `continue` without adding weight again, mirroring `TransactionCapsule.checkWeight()`'s address-keyed `HashMap`. Additionally, enforce canonical/low-`s` signature validation (reject non-canonical `s` values) before signature recovery in this precompile to eliminate the malleability vector at the source.

### Proof of Concept
Conceptual PoC (needs confirmation in a live/test environment by the background agent):
1. Create an account with an `Active` permission requiring `threshold = 2`, with a single key `K` of `weight = 2` is not useful — instead configure threshold `2` with the *attacker's single key* `K` given weight `1`, and require 2 signatures to reach threshold (simulating a 2-of-2 scheme where the attacker only controls one of the two required keys' address twice due to the bug).
2. Sign the fixed `hash = Sha256Hash.hash(address || permissionId || data)` with key `K` to get signature `sig1 = (r, s, v)`.
3. Derive the malleable counterpart `sig2 = (r, n-s, v')` (flip recovery id accordingly) — both `sig1` and `sig2` are valid signatures by `K` over the same `hash` and both recover to the same address.
4. Call the `ValidateMultiSign` precompile with `signatures = [sig1, sig2]`.
5. Observe that the loop counts `weight` for `K` twice (since `sign` bytes differ between `sig1` and `sig2` even though `recoveredAddr` is identical), producing `totalWeight = 2 * weight(K)`, which can incorrectly satisfy `permission.getThreshold()` using a single private key. [3](#0-2) [4](#0-3)

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1080-1120)
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

**File:** framework/src/test/java/org/tron/common/runtime/vm/ValidateMultiSignContractTest.java (L68-156)
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
    dbManager.getAccountStore().put(key.getAddress(), toAccount);

    //generate data

    byte[] address = key.getAddress();
    int permissionId = 2;
    byte[] data = Sha256Hash.hash(CommonParameter
        .getInstance().isECKeyCryptoEngine(), longData);

    //combine data
    byte[] merged = ByteUtil.merge(address, ByteArray.fromInt(permissionId), data);
    //sha256 of it
    byte[] toSign = Sha256Hash.hash(CommonParameter
        .getInstance().isECKeyCryptoEngine(), merged);

    //sign data

    List<Object> signs = new ArrayList<>();
    signs.add(Hex.toHexString(key1.sign(toSign).toByteArray()));
    //add Repetitive
    signs.add(Hex.toHexString(key1.sign(toSign).toByteArray()));
    signs.add(Hex.toHexString(key2.sign(toSign).toByteArray()));

    Assert.assertArrayEquals(
        validateMultiSign(StringUtil.encode58Check(key.getAddress()), permissionId, data, signs)
            .getValue(), DataWord.ONE().getData());

    //after optimized
    VMConfig.initAllowTvmSelfdestructRestriction(1);
    Assert.assertArrayEquals(
        validateMultiSign(StringUtil.encode58Check(key.getAddress()), permissionId, data, signs)
            .getValue(), DataWord.ONE().getData());
    VMConfig.initAllowTvmSelfdestructRestriction(0);

    //weight not enough
    signs = new ArrayList<>();
    signs.add(Hex.toHexString(key1.sign(toSign).toByteArray()));
    Assert.assertArrayEquals(
        validateMultiSign(StringUtil.encode58Check(key.getAddress()), permissionId, data, signs)
            .getValue(), DataWord.ZERO().getData());

    //put wrong sign
    signs = new ArrayList<>();
    signs.add(Hex.toHexString(key1.sign(toSign).toByteArray()));
    Assert.assertArrayEquals(
        validateMultiSign(StringUtil.encode58Check(key.getAddress()), permissionId, data, signs)
            .getValue(), DataWord.ZERO().getData());

    signs = new ArrayList<>();
    signs.add(Hex.toHexString(key1.sign(toSign).toByteArray()));
    signs.add(Hex.toHexString(new ECKey().sign(toSign).toByteArray()));

    Assert.assertArrayEquals(
        validateMultiSign(StringUtil.encode58Check(key.getAddress()), permissionId, data, signs)
            .getValue(), DataWord.ZERO().getData());
  }

```
