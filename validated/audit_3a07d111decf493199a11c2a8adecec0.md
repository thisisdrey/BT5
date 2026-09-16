### Title
Signature/address recycling in `ValidateMultiSign` precompile allows single key to double-count weight and bypass multisig threshold - ([File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java])

### Summary
The `ValidateMultiSign` TVM precompiled contract (address callable from any smart contract via `TriggerSmartContract`) sums signature weights against a `Permission`'s threshold, but its de-duplication logic keys off the exact signature byte-string plus recovered address pair, not the recovered signer address alone. Because ECDSA signatures are malleable (a signature `(r,s)` and its counterpart `(r, n-s)` with the complementary recovery id both recover to the same address but are different byte strings), or simply because a signer can produce a second differently-encoded valid signature over the same hash, a single private key can be represented by two or more distinct byte-level "signatures" that each pass the per-signature weight check, letting one key's weight be counted multiple times toward the threshold.

### Finding Description
`ValidateMultiSign.execute` iterates over the caller-supplied signature array, recovers the signer address for each, and only skips the weight addition when **the exact byte-encoded signature has already been seen for that recovered address**: [1](#0-0) 

Specifically:
```
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
When the same `recoveredAddr` reappears but the raw `sign` bytes differ from any previously seen entry, the code does **not** `continue` — it falls through and adds the address's weight to `totalWeight` again. This is the same root-cause class as the reported bug: recycling of a single signer identity across multiple "slots" (here, multiple differently-encoded signatures instead of duplicate owner-array indices) to inflate the counted weight beyond what the unique-signer set actually authorizes.

By contrast, the transaction-level multisig check `TransactionCapsule.checkWeight`, used by ordinary transaction broadcast/validation and by `getTransactionSignWeight`/`getTransactionApprovedList`, correctly de-duplicates by **recovered address** via a `HashMap` keyed on the encoded address, rejecting a second signature from the same signer with `"... has signed twice!"`: [2](#0-1) 

`ValidateMultiSign` does not follow this pattern, so it is exposed to the same “signature recycling to reach threshold” bug class described in the external report, but reachable purely through TVM execution rather than an off-chain SDK path.

### Impact Explanation
`ValidateMultiSign` is designed for smart contracts to implement on-chain governance/authorization checks against a TRON account's `Permission` (multisig) configuration — e.g. a contract that gates a withdrawal, upgrade, or admin action on `totalWeight >= permission.getThreshold()`. Any unprivileged address can call such a contract with a crafted `TriggerSmartContract` transaction. If an attacker controls just one key listed in the permission (even with a weight below the threshold), they can submit two distinct valid signature encodings over the same hash for that key, causing the precompile to report a passing weight sum despite only one actual authorized party having signed. This directly undermines the multisig guarantee for any contract relying on this precompile for authorization, enabling unauthorized execution of privileged/contract logic — a concrete unauthorized-account-operation impact.

### Likelihood Explanation
Exploitation only requires access to one private key that is part of the target `Permission`'s key list and the ability to produce a second differently-encoded valid ECDSA signature over the same message hash (achievable via signature malleability, i.e., flipping `s` to `n-s` and the recovery id, which requires no extra secret information — it is a pure mathematical transform of any existing valid signature). No special privileges, node compromise, or witness/SR control are needed; it is reachable by any ordinary contract caller.

### Recommendation
Change the deduplication key in `ValidateMultiSign.execute` (and the analogous `BatchValidateSign`/other precompiles if they share this pattern) to track **used signer addresses**, not signature byte-strings: once `recoveredAddr` has contributed weight once, skip counting it again regardless of the exact signature bytes supplied, mirroring the address-keyed `HashMap` approach already used in `TransactionCapsule.checkWeight`.

### Proof of Concept
1. Create an account with an `Active` permission containing one key `K` with weight `w1 < threshold` and possibly other keys.
2. Sign the constructed hash (`address || permissionId || data`, double-SHA256'd per `ValidateMultiSign`) once with `K`, producing signature `S1 = (r, s, v)`.
3. Derive the malleable counterpart `S2 = (r, n-s, 1-v)` (still a valid ECDSA signature recovering to the same address `K`).
4. Call the `ValidateMultiSign` precompile (via a `TriggerSmartContract` invoking a contract that forwards to the precompile, e.g. as exercised in `framework/src/test/java/org/tron/common/runtime/vm/ValidateMultiSignContractTest.java`) with `signatures = [S1, S2]`.
5. Because `S1 != S2` as byte arrays, the `matrixContains(executedSignList, sign)` check for the second entry fails to match, so the loop does not `continue`; `getWeight(permission, K)` is added a second time, and `totalWeight = 2*w1`, potentially exceeding the threshold with only one actual signer. [3](#0-2)

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1052-1120)
```java
    public Pair<Boolean, byte[]> execute(byte[] rawData) {
      if (VMConfig.allowTvmOsaka()
          && !isValidAbiEncoding(rawData, ABI_HEADER_WORDS, ABI_ITEM_WORDS)) {
        return Pair.of(false, EMPTY_BYTE_ARRAY);
      }
      DataWord[] words = DataWord.parseArray(rawData);
      byte[] address = words[0].toTronAddress();
      int permissionId = words[1].intValueSafe();
      byte[] data = words[2].getData();

      byte[] combine = ByteUtil.merge(address, ByteArray.fromInt(permissionId), data);
      byte[] hash = Sha256Hash.hash(CommonParameter
          .getInstance().isECKeyCryptoEngine(), combine);

      if (VMConfig.allowTvmSelfdestructRestriction()) {
        int sigArraySize = words[words[3].intValueSafe() / WORD_SIZE].intValueSafe();
        if (sigArraySize > MAX_SIZE) {
          return Pair.of(true, DATA_FALSE);
        }
      }
      byte[][] signatures = VMConfig.allowTvmSelfdestructRestriction() ?
          extractSigArray(words, words[3].intValueSafe() / WORD_SIZE, rawData) :
          extractBytesArray(words, words[3].intValueSafe() / WORD_SIZE, rawData);

      if (signatures.length == 0 || signatures.length > MAX_SIZE) {
        return Pair.of(true, DATA_FALSE);
      }

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
