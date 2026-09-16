## Finding

### Title
Unbounded memory growth via `ownerAddressSet` entries never released after multi-sign transaction processing - (File: `framework/src/main/java/org/tron/core/db/Manager.java`)

### Summary
`Manager` maintains an in-memory `Set<String> ownerAddressSet` that is populated every time a transaction whose contract carries a non-default permission ID (`isMultiSignTransaction`) is processed, but the codebase contains no removal/eviction path for entries in this set. This is the same bug class as the reported Zebra mempool `cancel_handles` leak: an in-memory tracking structure that is only ever appended to, with cleanup logic present for the normal/terminal paths of related structures (pending queue, re-push queue) but never for this particular map/set, causing monotonic, unbounded growth driven by ordinary, unprivileged transaction traffic.

### Finding Description
`ownerAddressSet` is declared as a plain in-memory `HashSet<String>`: [1](#0-0) 

It is populated inside `processTransaction()`, which runs for every transaction pushed to the pending pool (`pushTransaction()`/`rePush()`) as well as every transaction applied while producing or importing a block: [2](#0-1) 

The only consumers of `ownerAddressSet` are read-only lookups in `getVerifyTxs()`, `generateBlock()`, and `rePush()`: [3](#0-2) [4](#0-3) [5](#0-4) 

Unlike `pendingTransactions`/`rePushTransactions`, which are explicitly cleared or time-boxed by `PendingManager.close()`/`txIteration()`: [6](#0-5) 

there is no equivalent eviction for `ownerAddressSet`. No `.remove()` or `.clear()` call on this field exists anywhere in `Manager.java` outside of test code that mutates it via reflection. This is corroborated directly by an existing regression test's own comment, which documents that a transaction can be "no longer in pendingTransactions but `ownerAddressSet` still contains X": [7](#0-6) 

Just like the Zebra `cancel_handles` map — which is only cleaned by `cancel(mined_ids)` and thus leaks on the timeout path — `ownerAddressSet` is populated on every multi-sign-tagged transaction (pushed to pending, timed out, expired, or applied in a block) but has no corresponding cleanup for the pending/timeout/expiry cases, so entries accumulate forever for the lifetime of the process.

### Impact Explanation
Every distinct owner address that ever submits a transaction whose contract carries a non-zero `permissionId` (i.e., signed under an "active"/multi-sig permission rather than the default owner permission) permanently occupies an entry in this set — it is never reclaimed even after the transaction is mined, rejected, or expires from the pending pool. An attacker can cheaply generate large numbers of accounts and drive each one to submit at least one active-permission transaction (a standard, unprivileged, fee-paying operation any account can perform once it has configured a multi-sig `Permission` via `AccountPermissionUpdateContract`), causing `ownerAddressSet` to grow without bound over the node's uptime. This produces a slow, monotonic native-heap memory leak from ordinary broadcastable-transaction traffic, eventually leading to OOM-driven node crash/halt — the same class of impact (gradual unbounded memory exhaustion causing node termination) attributed to the reported Zebra advisory.

### Likelihood Explanation
High reachability, low cost per unit of leak: the attacker only needs standard, unprivileged capabilities — create an account, set an active permission via `AccountPermissionUpdateContract` (already gated by `AccountPermissionUpdateActuator.validate()` but not rate-limited against repetition across distinct addresses), and submit one further transaction signed with that non-default permission id via broadcastTransaction. Each such account contributes one permanent entry to `ownerAddressSet`. Since the growth is proportional to number of distinct addresses driven through this path rather than to any bounded resource (like block count or connection count), and there is no periodic GC, count cap, or time-based eviction, sustained low-cost transaction issuance over time will accumulate memory indefinitely, mirroring the "hours of sustained traffic to exhaust memory" profile from the original report.

### Recommendation
Add an eviction path for `ownerAddressSet` mirroring the two contexts where addresses lose relevance: (1) when the corresponding pending transaction is dropped/expired by `PendingManager` (currently only affects `pendingTransactions`/`rePushTransactions`), and (2) after a permission-changing transaction has been mined and is no longer relevant for cross-checking against future blocks (e.g., once the account's on-chain permission set stabilizes or after a bounded time window since the last use). Alternatively, replace the unbounded `HashSet<String>` with a bounded, time-expiring cache (e.g., Guava `Cache` with `maximumSize`/`expireAfterWrite`, consistent with the pattern already used for `transactionIdCache` in the same class) so stale owner-address entries are automatically reclaimed regardless of which code path (success, rejection, or timeout) removed the underlying transaction.

### Proof of Concept
1. Attacker creates account `A`, funds it minimally, and issues `AccountPermissionUpdateContract` to configure an `active` permission with `permissionId != 0` (allowed operation for any account owner; `AccountPermissionUpdateActuator.validate()` only checks structural validity, not repetition across distinct new accounts).
2. Attacker broadcasts one transaction from `A` whose `Transaction.Contract.permission_id` references that non-default permission — `isMultiSignTransaction()` returns true, and `processTransaction()` inserts `ByteArray.toHexString(A)` into `ownerAddressSet` (`Manager.java:1600-1602`), regardless of whether the transaction is subsequently mined, rejected, or expires out of `pendingTransactions`/`rePushTransactions` via `PendingManager`.
3. Repeat steps 1–2 for a large number of newly created addresses `A1..An`. Each iteration is cheap (bounded by transaction fee/bandwidth) and adds a permanent, unrecoverable entry to `ownerAddressSet`, since no code path in `Manager.java` ever calls `ownerAddressSet.remove(...)`.
4. Over sustained operation, `ownerAddressSet` grows monotonically with the number of distinct addresses driven through this path, consuming heap indefinitely until the node experiences memory pressure / OOM termination.

### Citations

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L235-235)
```java
  private Set<String> ownerAddressSet = new HashSet<>();
```

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L1246-1268)
```java
    List<TransactionCapsule> txs = new ArrayList<>();
    Map<String, TransactionCapsule> txMap = new HashMap<>();
    Set<String> multiAddresses = new HashSet<>(ownerAddressSet);

    pendingTransactions.forEach(capsule -> {
      String txId = Hex.toHexString(capsule.getTransactionId().getBytes());
      if (isMultiSignTransaction(capsule.getInstance())) {
        String address = Hex.toHexString(capsule.getOwnerAddress());
        multiAddresses.add(address);
      } else {
        txMap.put(txId, capsule);
      }
    });

    block.getTransactions().forEach(capsule -> {
      String address = Hex.toHexString(capsule.getOwnerAddress());
      String txId = Hex.toHexString(capsule.getTransactionId().getBytes());
      if (multiAddresses.contains(address) || !isSameSig(capsule, txMap.get(txId))) {
        txs.add(capsule);
      } else {
        capsule.setVerified(true);
      }
    });
```

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L1600-1602)
```java
    if (isMultiSignTransaction(trxCap.getInstance())) {
      ownerAddressSet.add(ByteArray.toHexString(trxCap.getOwnerAddress()));
    }
```

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L1721-1738)
```java
      //multi sign transaction
      byte[] owner = trx.getOwnerAddress();
      String ownerAddress = ByteArray.toHexString(owner);
      if (accountSet.contains(ownerAddress)) {
        continue;
      } else {
        if (isMultiSignTransaction(transaction)) {
          accountSet.add(ownerAddress);
        }
      }

      if (isExchangeTransaction(transaction)) {
        continue;
      }

      if (ownerAddressSet.contains(ownerAddress)) {
        trx.setVerified(false);
      }
```

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L2134-2144)
```java
  public void rePush(TransactionCapsule tx) {
    if (containsTransaction(tx)) {
      return;
    }

    String ownerAddress = ByteArray.toHexString(tx.getOwnerAddress());
    synchronized (this) {
      if (ownerAddressSet.contains(ownerAddress)) {
        tx.setVerified(false);
      }
    }
```

**File:** framework/src/main/java/org/tron/core/db/PendingManager.java (L23-60)
```java
  @Override
  public void close() {

    long now = System.currentTimeMillis();
    Iterator<TransactionCapsule> iterator = dbManager.getRePushTransactions().iterator();
    while (iterator.hasNext()) {
      TransactionCapsule tx = iterator.next();
      if (now - tx.getTime() > timeout) {
        iterator.remove();
        Metrics.gaugeInc(MetricKeys.Gauge.MANAGER_QUEUE, -1,
            MetricLabels.Gauge.QUEUE_REPUSH);
        Metrics.counterInc(MetricKeys.Counter.TXS, 1,
            MetricLabels.Counter.TXS_FAIL, MetricLabels.Counter.TXS_FAIL_TIMEOUT);
        if (Args.getInstance().isOpenPrintLog()) {
          logger.warn("Timeout remove tx from repush, txId: {}.", tx.getTransactionId());
        }
      }
    }

    for (TransactionCapsule tx : dbManager.getPendingTransactions()) {
      txIteration(tx);
    }

    dbManager.getPendingTransactions().clear();
    Metrics.gaugeSet(MetricKeys.Gauge.MANAGER_QUEUE, 0,
        MetricLabels.Gauge.QUEUE_PENDING);
    for (TransactionCapsule tx : dbManager.getPoppedTransactions()) {
      tx.setTime(System.currentTimeMillis());
      txIteration(tx);
    }
    dbManager.getPoppedTransactions().clear();
    Metrics.gaugeSet(MetricKeys.Gauge.MANAGER_QUEUE, 0,
        MetricLabels.Gauge.QUEUE_POPPED);
    if (Args.getInstance().isOpenPrintLog()) {
      logger.info("Pending tx size: {}.", dbManager.getRePushTransactions().size());
    }

  }
```

**File:** framework/src/test/java/org/tron/core/db/ManagerTest.java (L890-898)
```java
  @Test
  public void getVerifyTxsSkipsBlockWhenPermissionTxAlreadyConsumed() throws Exception {
    // Scenario: a permission-change tx (A) for owner X has been processed and consumed,
    // so it is no longer in pendingTransactions but ownerAddressSet still contains X.
    // A later transfer tx (B) from X with the old signature enters pending with
    // isVerified=true. A malicious SR produces a block containing only B (no A).
    // getVerifyTxs must place B into the re-verify list rather than calling
    // setVerified(true) just because B matches the pending entry.
    TransferContract bContract = TransferContract.newBuilder()
```
