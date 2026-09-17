Confirmed: `transactionIdCache` has no `invalidate`/`remove` calls anywhere in the codebase, so the entry added in `Wallet.broadcastTransaction` at line 564 is never cleaned up regardless of how the subsequent validation fails.

### Title
Incomplete cleanup of `transactionIdCache` on failed broadcast enables permanent DUP_TRANSACTION_ERROR censorship of a known txID - (File: framework/src/main/java/org/tron/core/Wallet.java)

### Summary
`Wallet.broadcastTransaction` inserts the transaction's ID into `Manager#transactionIdCache` before signature and full contract/actuator validation completes, and never removes it if that later validation fails. An anonymous API caller who knows (or can predict/observe) the exact `rawData` of a transaction — and therefore its `txID`, since TRON's transaction ID is derived from `raw_data` and does not depend on a valid signature — can pre-poison a target node's cache entry for that txID with a deliberately broken variant (e.g. corrupted signature). This causes the node's `broadcastTransaction` endpoint to permanently reject (up to 1 hour, or until eviction/restart) the legitimate transaction with the same txID as a duplicate, even though it was never actually accepted or executed.

### Finding Description
In `Wallet.broadcastTransaction`: [1](#0-0) 
the txID is added to `dbManager.getTransactionIdCache()` immediately after the size-only signature check and before the crypto signature check and full actuator validation performed later in `Manager.pushTransaction`: [2](#0-1) 

If `pushTransaction` subsequently throws any of `ValidateSignatureException`, `ContractValidateException`, `ContractExeException`, `AccountResourceInsufficientException`, `TaposException`, `TooBigTransactionException`, `TransactionExpirationException`, or a generic `Exception`, the corresponding catch block in `broadcastTransaction` returns an error response but never invalidates the `transactionIdCache` entry: [3](#0-2) 

No code path anywhere in the repository calls `invalidate`/`remove` on `transactionIdCache`; it only expires naturally after 1 hour (`expireAfterWrite`) or is evicted at 100,000 entries capacity: [4](#0-3) 

Because TRON's `transactionId` is computed over `raw_data` only (independent of the signature bytes), an attacker who knows a transaction's `raw_data` (e.g., observed via P2P gossip of a pending broadcast, or a well-known/deterministic transaction from an exchange, bot, or DEX order flow) can submit a copy of that exact `raw_data` with a corrupted/invalid signature (still passing the size-only pre-check at lines 513–520) directly to a target node's `broadcastTransaction` HTTP/gRPC endpoint. This poisons `transactionIdCache` with the shared txID before the legitimate signed transaction reaches that node.

### Impact Explanation
When the legitimate, correctly-signed transaction with the same txID is later submitted to the poisoned node (whether directly via API or via P2P propagation reaching the node's mempool acceptance path that also checks this cache), it is rejected as `DUP_TRANSACTION_ERROR` at: [1](#0-0) 
without ever being processed. This is an unauthenticated, remotely-triggerable API-level denial of service against a specific transaction/account, causing that node's broadcast endpoint to durably refuse a legitimate operation for up to an hour, and can be repeated to indefinitely censor a target txID on that node. This maps to the CWE-460/CWE-471 "incomplete cleanup / modification of assumed-immutable cache" root cause of the referenced Zebra advisory, applied to java-tron's `Wallet`/`Manager` transaction-broadcast API surface rather than to P2P block sync.

### Likelihood Explanation
The attacker needs only network access to the node's public `broadcastTransaction` API (HTTP `/wallet/broadcasttransaction`, gRPC, or JSON-RPC) and knowledge of the target `raw_data` bytes, which is often predictable or directly observable (e.g., via mempool/P2P transaction gossip prior to confirmation, or well-known automated transaction formats). No privileged role, mining/SR power, or crypto break is required — only a syntactically valid-length but cryptographically invalid signature attached to the known `raw_data`.

### Recommendation
Remove the `transactionIdCache` entry in every failure branch of `broadcastTransaction` (all catch blocks), or move the cache insertion to occur only after `dbManager.pushTransaction(trx)` succeeds, mirroring the upstream Zebra fix that removes stale sent-hash entries on every failed write path.

### Proof of Concept
1. Observe or predict a transaction's `raw_data` `R` intended for account `A` (txID = hash(`R`)).
2. Construct a `Transaction` with `raw_data = R` and an invalid/garbage signature of valid byte length.
3. Submit via `POST /wallet/broadcasttransaction` (or gRPC `broadcastTransaction`) to the target node; `SignUtils.isValidLength` passes, `transactionIdCache.put(txID, true)` executes, then `pushTransaction` throws `ValidateSignatureException`, returned as `SIGERROR`.
4. Submit the real, correctly-signed transaction with the same `raw_data` to the same node.
5. Observe response `DUP_TRANSACTION_ERROR`, confirming the legitimate transaction is rejected purely due to the unremoved cache entry from step 3, for up to 1 hour.

### Citations

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

**File:** framework/src/main/java/org/tron/core/Wallet.java (L585-629)
```java
    } catch (ValidateSignatureException e) {
      logger.info(BROADCAST_TRANS_FAILED, txID, e.getMessage());
      return builder.setResult(false).setCode(response_code.SIGERROR)
          .setMessage(ByteString.copyFromUtf8("Validate signature error: " + e.getMessage()))
          .build();
    } catch (ContractValidateException e) {
      logger.info(BROADCAST_TRANS_FAILED, txID, e.getMessage());
      return builder.setResult(false).setCode(response_code.CONTRACT_VALIDATE_ERROR)
          .setMessage(ByteString.copyFromUtf8(CONTRACT_VALIDATE_ERROR + e.getMessage()))
          .build();
    } catch (ContractExeException e) {
      logger.info(BROADCAST_TRANS_FAILED, txID, e.getMessage());
      return builder.setResult(false).setCode(response_code.CONTRACT_EXE_ERROR)
          .setMessage(ByteString.copyFromUtf8("Contract execute error : " + e.getMessage()))
          .build();
    } catch (AccountResourceInsufficientException e) {
      logger.info(BROADCAST_TRANS_FAILED, txID, e.getMessage());
      return builder.setResult(false).setCode(response_code.BANDWITH_ERROR)
          .setMessage(ByteString.copyFromUtf8("Account resource insufficient error."))
          .build();
    } catch (DupTransactionException e) {
      logger.info(BROADCAST_TRANS_FAILED, txID, e.getMessage());
      return builder.setResult(false).setCode(response_code.DUP_TRANSACTION_ERROR)
          .setMessage(ByteString.copyFromUtf8("Dup transaction."))
          .build();
    } catch (TaposException e) {
      logger.info(BROADCAST_TRANS_FAILED, txID, e.getMessage());
      return builder.setResult(false).setCode(response_code.TAPOS_ERROR)
          .setMessage(ByteString.copyFromUtf8("Tapos check error."))
          .build();
    } catch (TooBigTransactionException e) {
      logger.info(BROADCAST_TRANS_FAILED, txID, e.getMessage());
      return builder.setResult(false).setCode(response_code.TOO_BIG_TRANSACTION_ERROR)
          .setMessage(ByteString.copyFromUtf8(e.getMessage())).build();
    } catch (TransactionExpirationException e) {
      logger.info(BROADCAST_TRANS_FAILED, txID, e.getMessage());
      return builder.setResult(false).setCode(response_code.TRANSACTION_EXPIRATION_ERROR)
          .setMessage(ByteString.copyFromUtf8("Transaction expired"))
          .build();
    } catch (Exception e) {
      logger.warn("Broadcast transaction {} failed", txID, e);
      return builder.setResult(false).setCode(response_code.OTHER_ERROR)
          .setMessage(ByteString.copyFromUtf8("Error: " + e.getMessage()))
          .build();
    }
```

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L226-230)
```java
  private BlockingQueue<TransactionCapsule> pushTransactionQueue = new LinkedBlockingQueue<>();
  @Getter
  private Cache<Sha256Hash, Boolean> transactionIdCache = CacheBuilder
      .newBuilder().maximumSize(TX_ID_CACHE_SIZE)
      .expireAfterWrite(1, TimeUnit.HOURS).recordStats().build();
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
