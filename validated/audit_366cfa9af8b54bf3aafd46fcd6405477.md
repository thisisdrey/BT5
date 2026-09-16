Confirmed the vulnerability: `Wallet.broadcastTransaction` inserts the transaction's ID into `transactionIdCache` **before** the signature is actually verified by `dbManager.pushTransaction(trx)`. Since `TransactionCapsule.getTransactionId()` is a hash of `rawData` only (it excludes the signature list), an attacker who observes any transaction's `rawData` (e.g. from the public mempool, an API response, or by simply constructing rawData for a target address/amount) can broadcast a copy with a syntactically valid but cryptographically bogus signature and "poison" the cache entry for that txID, causing the legitimate transaction to be rejected as a duplicate.

### Title
Broadcast transaction ID cache poisoning via unauthenticated (invalid-signature) resubmission causes silent drop of legitimate transactions - (File: framework/src/main/java/org/tron/core/Wallet.java)

### Summary
`Wallet.broadcastTransaction` computes `txID = trx.getTransactionId()` (a hash over `rawData`, independent of the signature) and inserts it into `dbManager.getTransactionIdCache()` **prior to** calling `dbManager.pushTransaction(trx)`, which is the method that actually verifies the signature. This ordering lets an unauthenticated caller "consume" the dedup slot for someone else's transaction with an invalid signature, causing the genuine, correctly-signed transaction to be rejected later as `DUP_TRANSACTION_ERROR`.

### Finding Description [1](#0-0) 
The signature check performed inline in `broadcastTransaction` only validates the *length* of each signature (via `SignUtils.isValidLength`), not its cryptographic correctness: [2](#0-1) 

Immediately after that length-only check (and before any expiration/contract/signature-correctness validation), the transaction ID is written into the dedup cache: [3](#0-2) 

Only afterwards is real cryptographic signature verification performed, inside `dbManager.pushTransaction(trx)` → `TransactionCapsule.validateSignature` → `validatePubSignature`: [4](#0-3) [5](#0-4) 

Critically, `getTransactionId()` is derived from `rawData` only and the signature is never part of the hash, so a `rawData` payload copied verbatim from a legitimate, already-signed transaction (visible on the wire, in mempool broadcasts, or reconstructible from a `TransferContract`/any actuator's public fields) will produce the identical `txID` regardless of what signature bytes are attached to it. An attacker who submits that `rawData` with any 65–68 byte garbage signature will:
1. Pass the length-only check.
2. Get `txID` inserted into `transactionIdCache` as "seen".
3. Fail `dbManager.pushTransaction` with `ValidateSignatureException` → the attacker gets `SIGERROR`, but the cache entry is never removed (there is no `invalidate()`/`remove()` call in the exception-handling paths of `broadcastTransaction`).

If the legitimate signer subsequently (or concurrently) submits the correctly-signed version of the same `rawData`, `broadcastTransaction` will find `txID` already present in `transactionIdCache` and return `DUP_TRANSACTION_ERROR`, silently discarding the honest transaction without ever reaching `dbManager.pushTransaction`, `Manager.processTransaction`, or the P2P layer.

This mirrors the structural bug class in CVE-2015-7979: an unauthenticated party sends a message with invalid authentication that nonetheless causes the legitimate, correctly-authenticated party's session/state to be torn down (in NTP, the association; here, the pending-broadcast slot for a specific transaction ID).

### Impact Explanation
This is a targeted denial-of-service against arbitrary broadcast transactions/contract calls/asset issuances/order placements: any unprivileged network participant who can predict or observe a victim's `rawData` (which is unsigned, hashable, and often predictable — e.g., known owner address, known nonce/expiration window, known contract parameters) can preemptively "claim" its txID in the cache, causing the victim's legitimately signed broadcast to be dropped with `DUP_TRANSACTION_ERROR` before it is even considered by `Manager.pushTransaction`. Because `checkExpiration` and TAPOS binding tie transactions to a narrow validity window, a griefer repeating this against a target address can reliably prevent that address's transactions (transfers, freezes, votes, order placements, contract calls) from ever entering the mempool during that window — a form of permanent-for-that-attempt fund/action freezing without needing the victim's key.

### Likelihood Explanation
High reachability: any anonymous HTTP/gRPC client calling `broadcastTransaction` can trigger this with zero cost beyond constructing a plausible `rawData` (reconstructable from public account/contract state, e.g., predictable `ref_block_bytes`/`ref_block_hash`/`expiration`/`timestamp` fields many wallets compute deterministically, or simply captured from an earlier failed/observed broadcast of the same rawData). No special privileges, signing keys, or SR/witness status are required — it only needs API access to `broadcastTransaction`, matching the "unprivileged transaction broadcaster" reachability the assessment is scoped to.

### Recommendation
Move the `transactionIdCache` population in `Wallet.broadcastTransaction` to occur *after* `dbManager.pushTransaction(trx)` succeeds (i.e., only cache txIDs that pass full signature/permission verification), or alternatively verify the transaction's cryptographic signature validity before inserting into the cache. Additionally, ensure any exception paths that occur after the cache insertion (`ValidateSignatureException`, `ContractValidateException`, etc.) explicitly invalidate/remove the just-inserted cache entry so a failed submission can never block a subsequent legitimate one for the same `rawData`.

### Proof of Concept
1. Observe or construct the `rawData` of a target transaction (e.g., a `TransferContract` from victim address `V` with a specific `ref_block_num`/`expiration`), without needing the victim's private key.
2. Attach any garbage 65-byte signature bytes to form a `Transaction`, and call `broadcastTransaction` (gRPC `Wallet.BroadcastTransaction` or the `/wallet/broadcasttransaction` HTTP endpoint) with it.
3. Observe: response is `SIGERROR`, but `dbManager.getTransactionIdCache()` now contains `txID = hash(rawData)`.
4. Have the victim (or simulate it) broadcast the real, correctly-signed transaction with the identical `rawData`.
5. Observe: response is `DUP_TRANSACTION_ERROR` — the legitimate transaction never reaches `Manager.pushTransaction`/`processTransaction`, and never propagates to peers, even though it never actually existed in the chain or mempool before.

### Citations

**File:** framework/src/main/java/org/tron/core/Wallet.java (L507-521)
```java
  public GrpcAPI.Return broadcastTransaction(Transaction signedTransaction) {
    GrpcAPI.Return.Builder builder = GrpcAPI.Return.newBuilder();
    TransactionCapsule trx = new TransactionCapsule(signedTransaction);
    trx.setTime(System.currentTimeMillis());
    Sha256Hash txID = trx.getTransactionId();
    try {
      for (ByteString sig : signedTransaction.getSignatureList()) {
        if (!SignUtils.isValidLength(sig.size())) {
          String info = "Signature size is " + sig.size();
          logger.warn("Broadcast transaction {} has failed, {}.", txID, info);
          return builder.setResult(false).setCode(response_code.SIGERROR)
              .setMessage(ByteString.copyFromUtf8("Validate signature error: " + info))
              .build();
        }
      }
```

**File:** framework/src/main/java/org/tron/core/Wallet.java (L558-566)
```java
      if (trxCacheEnable) {
        if (dbManager.getTransactionIdCache().getIfPresent(txID) != null) {
          logger.warn("Broadcast transaction {} has failed, it already exists.", txID);
          return builder.setResult(false).setCode(response_code.DUP_TRANSACTION_ERROR)
              .setMessage(ByteString.copyFromUtf8("Transaction already exists.")).build();
        } else {
          dbManager.getTransactionIdCache().put(txID, true);
        }
      }
```

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L904-909)
```java
    try {
      if (!trx.validateSignature(chainBaseManager.getAccountStore(),
          chainBaseManager.getDynamicPropertiesStore())) {
        throw new ValidateSignatureException(String.format("trans sig validate failed, id: %s",
            trx.getTransactionId()));
      }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/TransactionCapsule.java (L650-680)
```java
  public boolean validatePubSignature(AccountStore accountStore,
      DynamicPropertiesStore dynamicPropertiesStore)
      throws ValidateSignatureException {
    if (!isVerified) {
      if (this.transaction.getSignatureCount() <= 0
              || this.transaction.getRawData().getContractCount() <= 0) {
        throw new ValidateSignatureException("miss sig or contract");
      }
      if (this.transaction.getSignatureCount() > dynamicPropertiesStore
              .getTotalSignNum()) {
        throw new ValidateSignatureException("too many signatures");
      }

      byte[] hash = getTransactionId().getBytes();

      long startNs = System.nanoTime();
      try {
        if (!validateSignature(this.transaction, hash, accountStore, dynamicPropertiesStore)) {
          isVerified = false;
          throw new ValidateSignatureException("sig error");
        }
      } catch (SignatureException | PermissionException | SignatureFormatException e) {
        isVerified = false;
        throw new ValidateSignatureException(e.getMessage());
      } finally {
        logSlowSigVerify(startNs);
      }
      isVerified = true;
    }
    return true;
  }
```
