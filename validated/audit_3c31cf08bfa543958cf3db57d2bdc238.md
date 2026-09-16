Confirmed: no `invalidate()` call exists anywhere on the failure paths, so the `transactionIdCache` entry set at `Wallet.java:564` is never removed when the subsequent validation/execution steps fail.

### Title
Broadcast-time transaction ID cache is poisoned on validation failure, permanently blocking legitimate re-broadcast of an unconfirmed transaction for up to 1 hour - (File: `framework/src/main/java/org/tron/core/Wallet.java`)

### Summary
`Wallet.broadcastTransaction` inserts the transaction's `txID` into `dbManager.getTransactionIdCache()` *before* the transaction has actually passed signature validation, expiration checking, or actuator validate/execute. If any of these later steps fail, the cache entry is never cleaned up. Because `getTransactionIdCache()` is checked unconditionally on every future broadcast attempt and treated as proof the transaction "already exists," any account whose transaction hits a transient, retryable failure on first submission becomes unable to re-broadcast that exact transaction for up to one hour, even though it was never actually accepted into the pending pool or persisted on chain.

### Finding Description
In `Wallet.broadcastTransaction`: [1](#0-0) 
the code does:
```
if (trxCacheEnable) {
  if (dbManager.getTransactionIdCache().getIfPresent(txID) != null) {
    ... return DUP_TRANSACTION_ERROR
  } else {
    dbManager.getTransactionIdCache().put(txID, true);
  }
}
```
This `put` happens *before* `trx.checkExpiration(...)` and `dbManager.pushTransaction(trx)` are invoked: [2](#0-1) 
`dbManager.pushTransaction` performs real signature verification and actuator `validate()`/`execute()` via `Manager.pushTransaction` / `processTransaction`: [3](#0-2) 

If any of these downstream checks throw (`ValidateSignatureException`, `ContractValidateException`, `ContractExeException`, `AccountResourceInsufficientException`, `TaposException`, `TooBigTransactionException`, `TransactionExpirationException`, or any other `Exception`), the surrounding `catch` blocks in `Wallet.broadcastTransaction` only return an error response — none of them evict the txID from `transactionIdCache`: [4](#0-3) 

The cache is a Guava `Cache<Sha256Hash, Boolean>` with a 1-hour write expiry and no other eviction path, confirmed by the field definition in `Manager`: [5](#0-4) 

A search of the codebase confirms there is no `invalidate()` call on this cache anywhere, so once poisoned, the entry can only be cleared by the 1-hour TTL. Because Tron transaction IDs are deterministic hashes over the signed raw transaction bytes, any submission of the *identical* bytes (the same client retrying, or a node re-broadcasting the same signed tx it heard via P2P) after the first attempt failed for a transient reason (e.g., temporary `AccountResourceInsufficientException` due to bandwidth/energy exhaustion, a `TaposException` due to reference-block drift on a lagging node, or a boundary `TransactionExpirationException`) will be unconditionally short-circuited as `DUP_TRANSACTION_ERROR` at line 561, without ever reaching real validation again — even though the transaction was never pushed into `pendingTransactions` nor written to the `TransactionStore`.

### Impact Explanation
An unprivileged transaction broadcaster (any API/gRPC client) can be denied service on their own exact transaction for up to an hour once it hits one first transient validation failure — a legitimate operation (e.g., a time-sensitive HTLC/atomic-swap redemption, an exchange order, a deadline-bound contract call) can be prevented from ever landing on chain within its validity/expiration window, resulting in funds effectively frozen or an opportunity permanently lost, since the wallet/client cannot resubmit the same signed bytes and typical wallets do not always re-sign with a new nonce-equivalent (Tron's `ref_block`/`expiration`/`timestamp` fields make each unique submission require a fresh signature, which end users/hardware wallets may not readily produce). This is a self-service node request path (`Wallet.broadcastTransaction`, reachable via `GrpcAPI`/HTTP `broadcastTransaction`), not privileged.

### Likelihood Explanation
High. `trxCacheEnable` is enabled by default in typical node configurations, and transient validation failures (temporary insufficient bandwidth/energy, TAPOS drift on busy or lagging nodes, marginal expiration timing) are common and easily triggerable by simply submitting a transaction under normal network/resource conditions — no attacker collusion or malicious peer is required; a legitimate client can trigger this against itself, and any observer who relays the exact same signed bytes to a node ahead of the original sender (e.g., via P2P sniffing since transactions propagate to all peers before confirmation) can deliberately induce the poisoning against a victim's own transaction.

### Recommendation
Either (a) only insert into `transactionIdCache` after `dbManager.pushTransaction` succeeds, or (b) explicitly `invalidate(txID)` in every `catch` block within `broadcastTransaction` so that a failed validation/execution attempt does not block future legitimate resubmission of the same transaction.

### Proof of Concept
1. Submit a signed `TransferContract` transaction to a node via `broadcastTransaction` at a moment when the sending account has temporarily insufficient bandwidth (or when the node's head block has drifted such that `validateTapos`/`checkExpiration` marginally fails) — the call reaches line 564 `dbManager.getTransactionIdCache().put(txID, true)` before failing later in `dbManager.pushTransaction` with e.g. `AccountResourceInsufficientException`.
2. The response returns `BANDWITH_ERROR` (or another failure code), but `txID` is now cached as `true`.
3. Wait for the resource/tapos condition to clear (a few seconds/blocks) and re-submit the *same* signed transaction bytes.
4. Observe the broadcast is rejected immediately with `DUP_TRANSACTION_ERROR` at `Wallet.java:561`, even though `chainBaseManager.getTransactionStore().has(txID)` is `false` and the transaction was never added to `pendingTransactions` — it cannot be resubmitted again until the 1-hour cache TTL expires.

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

**File:** framework/src/main/java/org/tron/core/Wallet.java (L568-576)
```java
      if (chainBaseManager.getDynamicPropertiesStore().supportVM()) {
        trx.resetResult();
      }
      if (trx.getInstance().getRawData().getContractCount() == 0) {
        throw new ContractValidateException(ActuatorConstant.CONTRACT_NOT_EXIST);
      }
      trx.checkExpiration(chainBaseManager.getNextBlockSlotTime());
      dbManager.pushTransaction(trx);
      TransactionMessage message = new TransactionMessage(trx.getInstance().toByteArray());
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

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L886-909)
```java
  public boolean pushTransaction(final TransactionCapsule trx)
      throws ValidateSignatureException, ContractValidateException, ContractExeException,
      AccountResourceInsufficientException, DupTransactionException, TaposException,
      TooBigTransactionException, TransactionExpirationException,
      ReceiptCheckErrException, VMIllegalException, TooBigTransactionResultException {

    if (isShieldedTransaction(trx.getInstance()) && !chainBaseManager.getDynamicPropertiesStore()
        .supportShieldedTransaction()) {
      throw new ContractValidateException("ShieldedTransferContract is not supported.");
    }

    if (isExchangeTransaction(trx.getInstance())) {
      throw new ContractValidateException("ExchangeTransactionContract is rejected");
    }

    pushTransactionQueue.add(trx);
    Metrics.gaugeInc(MetricKeys.Gauge.MANAGER_QUEUE, 1,
        MetricLabels.Gauge.QUEUE_QUEUED);
    try {
      if (!trx.validateSignature(chainBaseManager.getAccountStore(),
          chainBaseManager.getDynamicPropertiesStore())) {
        throw new ValidateSignatureException(String.format("trans sig validate failed, id: %s",
            trx.getTransactionId()));
      }
```
