### Title
Weak 32-bit truncated hash used as `hashCode()` for `Sha256Hash` enables Hash-DoS against the in-memory duplicate-transaction cache - ([File: common/src/main/java/org/tron/common/utils/Sha256Hash.java])

### Summary
`Sha256Hash.hashCode()` derives the Java hash bucket value from only the **last 4 bytes** of the SHA-256 (or SM3) digest, instead of using the full 256-bit digest or a keyed/salted hash. This 32-bit-only hash is used as the key type for `Manager.transactionIdCache`, a Guava `Cache<Sha256Hash, Boolean>` that every broadcast transaction is checked against for duplicate-transaction detection. Because the transaction ID's last 4 bytes are fully attacker-controllable (an attacker can grind any mutable field — e.g. `expiration`, memo/data, fee_limit — to search for a target 32-bit suffix), an attacker can craft large numbers of transactions whose `hashCode()` values collide into the same bucket(s), just as picoquic's weak SCID hash allowed remote peers to force hash-table collisions.

### Finding Description
`Sha256Hash.hashCode()`: [1](#0-0) 
uses only 4 bytes (32 bits) of the digest as the Java object hash code, explicitly to avoid "the first 4 bytes [that] are often zeros" — but this reduces the effective collision space that an attacker must search from 2^256 to 2^32, and via the birthday bound an attacker only needs on the order of tens of thousands of grinding attempts to find several transactions colliding into the *same* bucket of a Guava-cache segment (whose internal table size is far smaller than 2^32).

This `Sha256Hash` type is the key of the in-memory duplicate-transaction cache in `Manager`: [2](#0-1) 

That cache is populated on every block application: [3](#0-2) 

and is queried on the **broadcast path** for every incoming transaction (confirmed by the mocked wallet test, which shows `Wallet.broadcastTransaction` calling `managerMock.getTransactionIdCache()` and rejecting duplicates via `DUP_TRANSACTION_ERROR`): [4](#0-3) 

An unprivileged, anonymous client broadcasting transactions via the HTTP/gRPC `broadcastTransaction` endpoint reaches this lookup on every call when `trxCacheEnable` is set. Because `hashCode()` collisions are cheap to grind (only 32 bits of search space, further reduced by the target table's much smaller modulus), an attacker can flood a small number of hash buckets with many transaction IDs, degrading the cache from average O(1) lookup to O(n) chained comparisons for every subsequent lookup that hits the poisoned bucket(s).

### Impact Explanation
This does not lead to fund theft, unauthorized account operations, or unbacked balances. Its impact is CPU-load amplification / potential service degradation on the transaction-broadcast hot path of a full node, analogous in class to the picoquic Hash-DoS: a poisoned bucket forces repeated linear/degraded comparisons on every `broadcastTransaction` call that touches that bucket, increasing CPU consumption disproportionately to the attacker's cost. Because `equals()` on `Sha256Hash` performs a full 32-byte `Arrays.equals` comparison rather than being trivially cheap, and because this check happens per-broadcast under lock contention paths in `Manager.pushTransaction`, sustained abuse could measurably raise per-request latency across the node's transaction-intake path. This is a resource/performance degradation issue, not a memory-safety, consensus, or fund-safety issue.

### Likelihood Explanation
Medium-low. Reaching the vulnerable lookup requires only broadcasting syntactically valid, signed transactions (any account can construct/sign these), so the attack surface is reachable by any anonymous transaction broadcaster. However, mounting a *meaningfully damaging* attack requires (a) grinding many transactions with colliding `hashCode()` suffixes, which is computationally nontrivial though far cheaper than breaking SHA-256, and (b) getting enough colliding entries placed into the same Guava-cache segment before the 1-hour TTL expires or the 100,000-entry cap evicts them. Java `HashMap`-style treeification does not automatically apply here since this is a Guava `CacheBuilder` cache (segment-based `LocalCache`), not a plain `java.util.HashMap`, so the mitigation Java 8 added for `HashMap` collision chains does not directly protect this structure.

### Recommendation
Change `Sha256Hash.hashCode()` to use a wider slice of the digest (or all 32 bytes via a proper mixing function such as `Arrays.hashCode(bytes)` or a keyed hash) rather than a fixed 4-byte suffix, so that grinding a 32-bit collision offers no attacker advantage in targeting cache buckets. If retaining the "top bits often zero" rationale, mix all 32 bytes into a 32-bit result rather than truncating to a fixed slice. Consider bounding the impact further by capping the number of pending duplicate-check lookups per unique account/IP.

### Proof of Concept
1. An attacker crafts many valid, minimal-fee transactions where only a nonce-like field (`data`, `expiration`, memo bytes, etc.) is varied.
2. For each candidate, the attacker computes the transaction ID (`Sha256Hash.of(...)`), reads the last 4 bytes, and only submits (or precomputes locally) transactions whose 4-byte suffix maps into a small set of target buckets of the `transactionIdCache` Guava-cache segment table.
3. The attacker repeatedly calls `broadcastTransaction` with these crafted transactions; each call invokes `Manager.getTransactionIdCache().getIfPresent(txID)` (per `WalletMockTest.testBroadcastTransactionAlreadyExists`), forcing costlier probing/compare operations for the poisoned buckets on the node's transaction-intake hot path, increasing CPU time per lookup relative to normal broadcast traffic.

### Citations

**File:** common/src/main/java/org/tron/common/utils/Sha256Hash.java (L289-294)
```java
  @Override
  public int hashCode() {
    // Use the last 4 bytes, not the first 4 which are often zeros in Bitcoin.
    return Ints
        .fromBytes(bytes[LENGTH - 4], bytes[LENGTH - 3], bytes[LENGTH - 2], bytes[LENGTH - 1]);
  }
```

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L227-230)
```java
  @Getter
  private Cache<Sha256Hash, Boolean> transactionIdCache = CacheBuilder
      .newBuilder().maximumSize(TX_ID_CACHE_SIZE)
      .expireAfterWrite(1, TimeUnit.HOURS).recordStats().build();
```

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L2032-2036)
```java
  private void updateTransHashCache(BlockCapsule block) {
    for (TransactionCapsule transactionCapsule : block.getTransactions()) {
      this.transactionIdCache.put(transactionCapsule.getTransactionId(), true);
    }
  }
```

**File:** framework/src/test/java/org/tron/core/WalletMockTest.java (L312-346)
```java
  @Test
  public void testBroadcastTransactionAlreadyExists() throws Exception {
    Wallet wallet = new Wallet();
    Protocol.Transaction transaction = Protocol.Transaction.newBuilder().build();
    TransactionCapsule trx = new TransactionCapsule(transaction);
    trx.setTime(System.currentTimeMillis());
    Sha256Hash txID = trx.getTransactionId();

    Cache<Sha256Hash, Boolean> transactionIdCache = CacheBuilder
        .newBuilder().maximumSize(10)
        .expireAfterWrite(1, TimeUnit.HOURS).recordStats().build();
    transactionIdCache.put(txID, true);

    TronNetDelegate tronNetDelegateMock = mock(TronNetDelegate.class);
    Manager managerMock = mock(Manager.class);
    when(tronNetDelegateMock.isBlockUnsolidified()).thenReturn(false);
    when(managerMock.isTooManyPending()).thenReturn(false);
    when(managerMock.getTransactionIdCache()).thenReturn(transactionIdCache);

    Field field = wallet.getClass().getDeclaredField("tronNetDelegate");
    field.setAccessible(true);
    field.set(wallet, tronNetDelegateMock);

    Field field2 = wallet.getClass().getDeclaredField("dbManager");
    field2.setAccessible(true);
    field2.set(wallet, managerMock);

    Field field3 = wallet.getClass().getDeclaredField("trxCacheEnable");
    field3.setAccessible(true);
    field3.set(wallet, true);

    GrpcAPI.Return ret = wallet.broadcastTransaction(transaction);

    assertEquals(GrpcAPI.Return.response_code.DUP_TRANSACTION_ERROR,
        ret.getCode());
```
