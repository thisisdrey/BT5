### Title
Quadratic-Complexity Block-Result Assembly via Repeated `toBuilder().add...().build()` in `TransactionRetCapsule` - (File: chainbase/src/main/java/org/tron/core/capsule/TransactionRetCapsule.java)

### Summary
`TransactionRetCapsule.addTransactionInfo()` mutates its backing protobuf message by calling `toBuilder().addTransactioninfo(result).build()` on every invocation [1](#0-0) . This is structurally the same anti-pattern as the reported `shell-quote` bug: each call reallocates and copies the entire growing repeated-field list instead of appending in place, giving O(n²) total work when the method is invoked once per element in a loop (as shown in the block-processing test harness that mirrors production usage, e.g. `blockCapsule1.getTransactions().forEach(tx -> ... transactionRetCapsule1.addTransactionInfo(...))`) [2](#0-1) . `BlockCapsule.addTransaction()` has the identical shape, rebuilding the whole `Block` message via `toBuilder().addTransactions(...).build()` per call rather than using the batch `addAllTransactions` path [3](#0-2) .

### Finding Description
Protobuf-generated `Message.Builder` objects created via `message.toBuilder()` copy the underlying repeated field storage on each `build()`/`toBuilder()` round trip for messages that don't share mutable state. When `addTransactionInfo` (or `addTransaction`) is called iteratively — once per transaction while a block's results/transactions are assembled — every call re-copies all previously accumulated entries before appending one more, exactly mirroring the `prev.concat(arg)` reduce pattern flagged in the `shell-quote` advisory. This turns an O(n) assembly of n transaction results into O(n²) copying operations, driven entirely by the number of transactions in a block, a value influenced by ordinary unprivileged transaction broadcasters filling blocks.

### Impact Explanation
Because this code path executes during ordinary block application/result construction — work every full node (including nodes serving the HTTP/gRPC/JSON-RPC query APIs) must perform for every block — an attacker who can get many small transactions packed into blocks increases the per-block bookkeeping cost quadratically instead of linearly. Sustained abuse degrades block-processing throughput node-wide, which can cause processing to fall behind block production, harming availability of the node's query and consensus-following paths.

### Likelihood Explanation
Reaching this path requires nothing beyond broadcasting ordinary transactions (transfers, contract calls, etc.) so that many of them land in the same block; no special privilege, malicious SR/witness role, or crafted malformed payload is needed — only volume, similar to how the `shell-quote` PoC needed only plain space-separated tokens with no shell metacharacters.

### Recommendation
Replace the per-element `toBuilder().addX().build()` calls in `TransactionRetCapsule.addTransactionInfo` and `BlockCapsule.addTransaction` with a single batched builder held across the whole assembly loop (build once, call `addTransactioninfo`/`addTransactions` repeatedly on the same builder, and only call `.build()` once at the end), or use the existing `addAllTransactionInfos`/`addAllTransactions` bulk APIs which already avoid the repeated-copy pattern [4](#0-3) .

### Proof of Concept
Given a block containing N transactions, if the result-assembly code calls `transactionRetCapsule.addTransactionInfo(info)` once per transaction (as exercised in `JsonrpcServiceTest`'s block-result construction loop) [5](#0-4) , each call triggers `toBuilder()` on the accumulated `TransactionRet`, which duplicates all previously stored `TransactionInfo` entries before appending the next one [1](#0-0) . Total copy operations across the loop scale as 1+2+...+N = O(N²), so doubling the number of transactions packed into a block roughly quadruples the bookkeeping cost for that block, independent of any linear cost the node already pays for verifying/executing those transactions.

### Citations

**File:** chainbase/src/main/java/org/tron/core/capsule/TransactionRetCapsule.java (L39-41)
```java
  public void addTransactionInfo(TransactionInfo result) {
    this.transactionRet = this.transactionRet.toBuilder().addTransactioninfo(result).build();
  }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/TransactionRetCapsule.java (L43-45)
```java
  public void addAllTransactionInfos(List<TransactionInfo> results) {
    this.transactionRet = this.transactionRet.toBuilder().addAllTransactioninfo(results).build();
  }
```

**File:** framework/src/test/java/org/tron/core/jsonrpc/JsonrpcServiceTest.java (L196-206)
```java
    TransactionRetCapsule transactionRetCapsule1 = new TransactionRetCapsule();
    blockCapsule1.getTransactions().forEach(tx -> {
      TransactionInfoCapsule transactionInfoCapsule = new TransactionInfoCapsule();
      transactionInfoCapsule.setId(tx.getTransactionId().getBytes());
      transactionInfoCapsule.setBlockNumber(blockCapsule1.getNum());
      transactionInfoCapsule.setBlockTimeStamp(blockCapsule1.getTimeStamp());
      transactionInfoCapsule.addAllLog(logs);
      transactionRetCapsule1.addTransactionInfo(transactionInfoCapsule.getInstance());
    });
    dbManager.getTransactionRetStore()
        .put(ByteArray.fromLong(blockCapsule1.getNum()), transactionRetCapsule1);
```

**File:** chainbase/src/main/java/org/tron/core/capsule/BlockCapsule.java (L145-148)
```java
  public void addTransaction(TransactionCapsule pendingTrx) {
    this.block = this.block.toBuilder().addTransactions(pendingTrx.getInstance()).build();
    getTransactions().add(pendingTrx);
  }
```
