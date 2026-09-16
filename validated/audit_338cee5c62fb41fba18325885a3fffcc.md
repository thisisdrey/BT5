### Title
Missing deadline/expiration in TVM `ValidateMultiSign` precompile signature hash allows indefinite replay of multisig authorizations - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The `ValidateMultiSign` TVM precompile builds the signed hash purely from `(address, permissionId, data)` with no deadline, timestamp, or nonce component baked into the primitive itself, so any threshold signature set collected against a given `data` payload remains valid to replay forever, with no built-in invalidation path — the same root-cause pattern described in the external report for `encodeTransactionData()`.

### Finding Description
`ValidateMultiSign.execute()` computes the hash to be verified as `sha256(address || permissionId || data)` and then checks that the supplied signatures reach the account permission's threshold weight, purely based on this static combination: [1](#0-0) 

The weight/threshold check itself only validates that recovered signer addresses are present in the account's permission with sufficient weight; it does not consult, require, or even accept any expiration/deadline or per-use nonce field as part of the hashed payload: [2](#0-1) 

This precompile is java-tron's officially supported building block for implementing multisig-style, off-chain-collected authorizations inside arbitrary TVM smart contracts (e.g. custody/payroll/withdrawal-approval contracts), analogous in purpose to `TransactionCapsule.checkWeight()` used for on-chain transaction signature verification: [3](#0-2) 

Crucially, ordinary java-tron `Transaction`s are protected against indefinite validity because `Manager.validateCommon()` enforces a bounded `expiration` field taken from the transaction's `raw` data, rejecting transactions whose expiration is outside `[headBlockTime, headBlockTime + MAXIMUM_TIME_UNTIL_EXPIRATION]`: [4](#0-3) 

`ValidateMultiSign`'s `data` parameter, however, is entirely opaque application data chosen by the calling contract — the precompile provides no protocol-level deadline or nonce mechanism analogous to the one enforced for native transactions. Any smart contract that follows the exact same pattern as the flagged `encodeTransactionData()`/permit-style bug (hashing business data and relying solely on `ValidateMultiSign` for authorization, without independently embedding and checking an expiry timestamp and a burn-after-use nonce) inherits an unbounded-validity, unrevokable authorization primitive directly from java-tron's own TVM feature set.

### Impact Explanation
Contracts built on top of `ValidateMultiSign` for custody/payroll/withdrawal-approval flows (the same use case as the reported `PayrollManager.sol`) can have a stale or mistakenly-collected threshold-signature set replayed to authorize a fund transfer or account operation long after it should have become invalid, since the precompile itself offers no expiry/deadline and no revocation path — only removing the signer entirely from the on-chain permission (an unrelated, heavier-weight operation) would stop it. This can result in unauthorized fund movement/theft when such stale authorizations are replayed.

### Likelihood Explanation
Any account can deploy a contract on the public TVM that uses this precompile via `TriggerSmartContract`, and any transaction broadcaster can invoke it — no special privilege is required. The precompile is a first-class, documented (TIP-based) java-tron feature intended precisely for this kind of authorization use case, making the exposure directly attributable to java-tron's primitive rather than to obscure misuse.

### Recommendation
Document and/or provide a companion mechanism (e.g., a reserved word/format requiring an embedded expiration timestamp and a per-use nonce as part of the hashed `data`, with an on-chain "used-hash" registry analogous to nonce invalidation) so that multisig authorizations validated via `ValidateMultiSign` cannot be replayed indefinitely, mirroring the bounded-expiration guarantee java-tron already provides for native transactions in `Manager.validateCommon()`.

### Proof of Concept
1. Deploy a contract that authorizes a fund transfer of amount `X` to `Y` if `ValidateMultiSign(address, permissionId, keccak256(X,Y), signatures)` returns true, using the pattern shown in `ValidateMultiSignContractTest.java`.
2. Multisig signers sign the hash of `(X, Y)` once to authorize a legitimate payment.
3. Because the hash contains no deadline or nonce, the same signature set can be resubmitted to the contract at any later time to trigger the same payment again (or, if a signer's device/key is later found to be compromised or the payment terms change, the old authorization is still honored) — reproducing the exact “transaction stays valid as long as it has not been executed / can't be invalidated” condition from the external report.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1057-1065)
```java
      DataWord[] words = DataWord.parseArray(rawData);
      byte[] address = words[0].toTronAddress();
      int permissionId = words[1].intValueSafe();
      byte[] data = words[2].getData();

      byte[] combine = ByteUtil.merge(address, ByteArray.fromInt(permissionId), data);
      byte[] hash = Sha256Hash.hash(CommonParameter
          .getInstance().isECKeyCryptoEngine(), combine);

```

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

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L846-858)
```java
    long transactionExpiration = transactionCapsule.getExpiration();
    long headBlockTime = chainBaseManager.getHeadBlockTimeStamp();
    if (transactionCapsule.isInBlock()
        && chainBaseManager.getDynamicPropertiesStore().allowConsensusLogicOptimization()) {
      transactionCapsule.checkExpiration(chainBaseManager.getNextBlockSlotTime());
    }
    if (transactionExpiration <= headBlockTime
        || transactionExpiration > headBlockTime + Constant.MAXIMUM_TIME_UNTIL_EXPIRATION) {
      throw new TransactionExpirationException(
          String.format(
          "Transaction expiration, transaction expiration time is %d, but headBlockTime is %d",
              transactionExpiration, headBlockTime));
    }
```
