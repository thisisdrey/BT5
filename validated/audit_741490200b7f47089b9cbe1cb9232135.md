### Title
Missing anti-replay claim binding (no nonce/expiry/caller domain separation) in `ValidateMultiSign`/`BatchValidateSign` TVM precompiles allows indefinite cross-contract replay of authorization signatures - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The `ValidateMultiSign` precompile (and its sibling `BatchValidateSign`) recovers signer addresses from caller-supplied signatures and checks them against an account's on-chain `Permission` weight/threshold, functioning as a signature-based authorization primitive for smart contracts — analogous to how `Ueberauth.Strategy.Apple.Token.payload/2` verifies an ID token's JWS signature. Just as the Apple strategy verified only the signature and never validated `exp`/`aud`/`iss` claims before trusting the token's `sub`, `ValidateMultiSign` verifies only the ECDSA signature over `hash(address || permissionId || data)` and enforces no analogous "claims": no expiration, no nonce/sequence, and no binding to the calling contract, chain id, or transaction context. Any contract that relies on this precompile as its sole authorization gate inherits an indefinite, cross-contract replay window for every signature it ever accepts.

### Finding Description
`ValidateMultiSign.execute` builds the message to verify purely from attacker-supplied calldata: [1](#0-0) 
It recovers each signer via `recoverAddrBySign(sign, hash)` and accepts the call as authorized once the summed `Permission` key weights reach the threshold: [2](#0-1) 

Unlike a normal java-tron `Transaction`, which is protected by `Manager.validateCommon`'s expiration window (`transactionExpiration`) and `validateTapos`'s `refBlockHash` binding before signatures are trusted: [3](#0-2) 
the `validatemultisign`/`batchvalidatesign` precompiles have none of this. The signed payload is only `address || permissionId || data` (or a raw caller-chosen `hash` for `BatchValidateSign`), with no expiration timestamp, no nonce/sequence counter, and no binding to the invoking contract's address or the current chain — no “claims” equivalent to `exp`/`aud`/`iss` are checked, exactly the missing-validation pattern in the referenced CVE. Test code confirms the signed message is caller-defined and reused verbatim across calls: [4](#0-3) 

Because the precompile checks only current permission membership/threshold at call time (not txid, not a per-use nonce), a signature that was valid once for a given `(address, permissionId, data)` tuple remains valid forever for anyone who has observed it (e.g., in a prior on-chain transaction, a mempool broadcast, or a leaked authorization payload), and — since the invoking contract's address is never part of the signed hash — it can be replayed against any other contract that independently calls `validateMultiSign`/`batchValidateSign` with the same tuple, enabling cross-application authorization reuse in the same way the CVE's missing `aud` check enabled cross-client account takeover.

### Impact Explanation
Any smart contract on TRON that uses `validateMultiSign`/`batchValidateSign` as an off-chain-signature authorization mechanism (e.g., custodial wallets, meta-transaction relayers, exchange withdrawal approval contracts) and does not itself embed a nonce/expiry in the `data` it signs is exposed to indefinite replay of previously-valid authorizations. An attacker who captures one valid authorization signature (from a public transaction, event log, or relayed calldata) can resubmit it repeatedly, or submit it to a different contract that performs the same permission check, to trigger unauthorized privileged operations (e.g., repeated fund withdrawals) without ever needing a new signature from the key holder. This can result in concrete unauthorized account operations and theft of funds from any contract's balance that gates the action behind this precompile's TRUE result, satisfying the "concrete unauthorized account operation, theft of funds" bar.

### Likelihood Explanation
Reachable by any unprivileged party through a normal `TriggerSmartContract` call to a deployed contract that internally invokes the precompile — no special privilege, no node/miner cooperation, and no dependency on off-chain infrastructure is required. The precompile itself provides no protection; whether the vulnerability is exploitable depends entirely on whether the calling contract's own `data` payload includes a nonce/expiry, which the protocol neither mandates nor documents as required in the verified fields, making misuse plausible and undetectable to third parties auditing only the precompile's contract-level guarantee.

### Recommendation
Document (and, where feasible, enforce at the precompile layer) that `data` passed to `validateMultiSign`/`batchValidateSign` must include caller-chosen nonce and expiration fields, and consider adding a native domain-separation input (calling contract address / chain id) to the hashed payload so a single signature cannot be replayed across unrelated contracts. At minimum, emit explicit guidance and audit warnings analogous to requiring `exp`, `aud`, and replay-protection in signature-based authorization primitives.

### Proof of Concept
1. A key `K` with active-permission weight ≥ threshold on account `A` signs `sha256(A || permissionId || data)` once to authorize a withdrawal in Contract `X`, which calls `validatemultisign(A, permissionId, data, [sig])` to gate a `transfer` of TRX/TRC20 to a recipient encoded elsewhere.
2. The signature `sig`, `A`, `permissionId`, and `data` become visible on-chain (call data of the transaction that invoked `X`).
3. Provided `K` is still on `A`'s permission with sufficient weight, an attacker resubmits the identical `(A, permissionId, data, [sig])` tuple to `X` again (or to any other contract `Y` performing the same check), and `ValidateMultiSign.execute` returns `true` again because nothing in the check — `recoverAddrBySign` + `TransactionCapsule.getWeight` — depends on transaction freshness or the calling contract: [5](#0-4) 
4. Contract `X`/`Y` executes the privileged action again, resulting in repeated unauthorized withdrawals from the same authorization signature.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1057-1064)
```java
      DataWord[] words = DataWord.parseArray(rawData);
      byte[] address = words[0].toTronAddress();
      int permissionId = words[1].intValueSafe();
      byte[] data = words[2].getData();

      byte[] combine = ByteUtil.merge(address, ByteArray.fromInt(permissionId), data);
      byte[] hash = Sha256Hash.hash(CommonParameter
          .getInstance().isECKeyCryptoEngine(), combine);
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1080-1110)
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

**File:** framework/src/test/java/org/tron/common/runtime/vm/ValidateMultiSignContractTest.java (L104-125)
```java
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
```
