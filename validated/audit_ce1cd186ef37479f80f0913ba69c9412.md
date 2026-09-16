### Title
Multi-signature threshold bypass via duplicate/malleable ECDSA signatures in `ValidateMultiSign` precompile - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The `ValidateMultiSign` TVM precompiled contract (invoked via the Solidity-callable `validatemultisign(address,uint256,bytes32,bytes[])` interface) computes a signer's total approval weight against a `Permission` threshold by iterating over an attacker-supplied array of signatures. Its de-duplication logic only rejects an entry when the *exact byte-identical* `(recoveredAddress, signature)` pair repeats; it does not reject a second, differently-encoded signature that recovers to an *already-counted* address. This is analogous to a signature-wrapping flaw: a single validly-signed element is "wrapped" (re-encoded) so the verifier treats it as an independent, additional signed statement, letting one legitimate signature be counted more than once toward a threshold.

### Finding Description
`ValidateMultiSign.execute()` at [1](#0-0)  loops over the caller-supplied `signatures` array:

```
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

The only true "already counted" guard is the inner check `ByteArray.matrixContains(executedSignList, sign)`, which does a byte-for-byte comparison of `merge(recoveredAddr, sign)` (see [2](#0-1) ). If the address was already seen but the raw signature bytes differ even slightly, the code falls through `MUtil.checkCPUTime()` (a CPU-budget check, not a security gate) and unconditionally adds `getWeight(permission, recoveredAddr)` to `totalWeight` again.

Because standard ECDSA signatures are malleable — for any valid `(r, s, v)` there exists a second valid `(r, n-s, 1-v)` that recovers to the exact same address without needing the private key — or simply because two independent signing operations over the same hash by the same key normally yield different raw bytes (non-deterministic `k` in `ECKey.sign`), an attacker who possesses **one** valid signature from a single co-signer can supply two syntactically different byte encodings of it. Both recover to the same address and each pass is counted as separate weight, letting `totalWeight` reach or exceed `permission.getThreshold()` using only one real distinct private key, instead of the number of independent signers the deployer/permission intended.

The historical equivalent bug in the plain transaction-signing path (`TransactionCapsule.checkWeight`) was already hardened by keying the de-duplication map on the recovered *address* once `ForkController.instance().pass(Parameter.ForkBlockVersionEnum.VERSION_4_7_1)` is active — see [3](#0-2) . The TVM precompile `ValidateMultiSign` (and its sibling `BatchValidateSign`, which has no de-dup at all across positions) never received the equivalent address-keyed protection, leaving the on-chain, smart-contract-facing path exploitable even though the network-level transaction-approval path is fixed for post-fork blocks.

### Impact Explanation
`validatemultisign` is a public precompile any deployed smart contract can call with attacker-controlled `bytes[] signatures` at essentially the address `0x...151d` (or equivalent constant). Contracts that rely on it to implement on-chain M-of-N authorization (e.g., custodial wallets, DAOs, DEX order-signing contracts, bridges) can have their intended threshold bypassed by an attacker who has legitimately obtained (or derives via signature malleability from) a single co-signer's signature, letting that one signature be replayed in two distinct byte encodings to satisfy a 2-of-N (or higher) requirement. This is a direct authorization/threshold bypass that can lead to unauthorized account operations or theft of funds controlled by contracts that gate execution on `ValidateMultiSign`'s result.

### Likelihood Explanation
Reachable by any unprivileged contract-deployer/caller with no special privileges — just knowledge of one valid signature over the hash being checked (which is often exposed anyway, e.g., through `getTransactionApprovedList`, `getTransactionSignWeight`, or simply by being one of the legitimate but insufficient signers). No malicious SR/witness/committee/peer role is required, and it does not depend on p2p or consensus behavior — it's a pure on-chain computation bug in a widely reachable precompile.

### Recommendation
De-duplicate by recovered address rather than by exact signature bytes in `ValidateMultiSign.execute()` (and audit `BatchValidateSign` for the same class of issue): once an address has contributed weight, any further signature recovering to that same address must be skipped entirely, mirroring the address-keyed fix already applied to `TransactionCapsule.checkWeight` post `VERSION_4_7_1`. Concretely, replace the inner `matrixContains(executedSignList, sign)` exact-byte check with an unconditional `continue` whenever `matrixContains(executedSignList, recoveredAddr)` is true, removing the "fall-through and add weight again" branch.

### Proof of Concept
1. Deploy a contract with an active `Permission` requiring 2 signers, `key1` and `key2`, each weight 1, threshold 2.
2. Obtain (or independently produce) one signature `sig1 = key1.sign(hash)`.
3. Produce a second, byte-different signature from the same key over the same `hash` — either by asking `key1` to sign again (non-deterministic `k` yields different `(r,s)`), or by applying the standard ECDSA malleability transform `(r, n-s, flipped-v)` to `sig1` without needing the private key at all.
4. Call `validatemultisign(address, permissionId, hash, [sig1, sig1_variant])`.
5. In `ValidateMultiSign.execute()`, both entries recover to `key1`'s address; the first is added (`totalWeight=1`); the second matches `matrixContains(executedSignList, recoveredAddr)==true` but `matrixContains(executedSignList, sign)==false` (different raw bytes), so it falls through and adds weight again (`totalWeight=2`).
6. Result: `totalWeight (2) >= threshold (2)` returns `dataOne()` (success) even though only `key1` — a single real signer — participated, bypassing the intended 2-of-2 requirement. This is directly observable in the existing test `ValidateMultiSignContractTest.testDifferentCase` at [4](#0-3) , which submits two signatures from `key1` (labeled "add Repetitive") plus one from `key2` and asserts success — the test cannot distinguish correct dedup from the double-count bug because the threshold is already met by `key1+key2`; isolating the same pattern with only `key1`'s two signatures and no `key2` signature (threshold 2, only one real signer) reveals the bypass.

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

**File:** chainbase/src/main/java/org/tron/core/capsule/TransactionCapsule.java (L233-263)
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
