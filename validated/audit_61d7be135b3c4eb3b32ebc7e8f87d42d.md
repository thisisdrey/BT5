### Title
Permissionless griefing of `ShieldedTransferContract` submissions via the global per-node pending-pool cap and the one-shielded-tx-per-block limit - ([File: framework/src/main/java/org/tron/core/db/Manager.java])

### Summary
`Manager.pushTransaction()` enforces a global, unauthenticated cap (`shieldedTransInPendingMaxCounts`, default 10) on the number of shielded (`ShieldedTransferContract`) transactions that may sit in a node's pending pool at once, and block assembly enforces a hard cap of exactly one shielded transaction per block (`SHIELDED_TRANS_IN_BLOCK_COUNTS = 1`). Both counters/limits are keyed only on "is this a shielded transaction," with no per-account bookkeeping or fee-priority ordering to protect a specific user's transaction. Any address can broadcast shielded transactions to occupy these shared, limited slots, causing another user's legitimate, otherwise-valid shielded transaction to be rejected from the mempool or to be indefinitely excluded from block inclusion. This mirrors the reported `startNewRound()` pattern: a permissionless, low-cost call establishes a shared "lock"/limited resource that an attacker can race to acquire specifically to make someone else's pending transaction fail.

### Finding Description
In `pushTransaction`, before a shielded transaction is admitted to the pending pool, the node checks a global in-memory counter: [1](#0-0) 
```
synchronized (this) {
  if (isShieldedTransaction(trx.getInstance())
          && shieldedTransInPendingCounts.get() >= shieldedTransInPendingMaxCounts) {
    return false;
  }
  ...
  if (isShieldedTransaction(trx.getInstance())) {
    shieldedTransInPendingCounts.incrementAndGet();
  }
}
```
`shieldedTransInPendingMaxCounts` defaults to a small value (10) and is configured globally, not per-account: [2](#0-1) 

Separately, block application enforces that at most one shielded transaction may be included per block: [3](#0-2) [4](#0-3) 
```
if (block.getTransactions().stream()
        .filter(tran -> isShieldedTransaction(tran.getInstance()))
        .count() > SHIELDED_TRANS_IN_BLOCK_COUNTS) {
  throw new BadBlockException(...);
}
```

Any unprivileged account can broadcast `ShieldedTransferContract` transactions at will — no special permission is required, just a valid signature and standard fee. Because:
1. The pending-pool admission check for shielded transactions is a single shared counter across all senders, and
2. Block producers may only ever pack one shielded transaction per block,

an attacker can continuously submit shielded transactions (their own, potentially throwaway/self-looping shielded transfers) to keep the shared counter at its cap and/or to consistently win the single per-block shielded-transaction slot ahead of a targeted victim's transaction (e.g., by broadcasting with tighter timing or simply flooding faster than the victim can get included). While the attacker's transactions occupy these slots, a legitimate victim's shielded transaction:
- is rejected outright by `pushTransaction` (returns `false`, i.e., silently dropped) when the pending pool cap is reached, or
- sits in the pending pool but can never be selected for a block because the one-shielded-tx-per-block slot is always taken by the attacker's own competing shielded transactions.

This is directly analogous to the reported `startNewRound()` issue: a cheap, permissionless call establishes a shared, limited "lock" state (front-running lock / one-slot-per-round in the original report; pending-pool cap / one-per-block slot here) that an attacker can repeatedly seize specifically to force another user's pending, otherwise-valid transaction to fail or never be included.

### Impact Explanation
An attacker can persistently and cheaply deny service to the `ShieldedTransferContract` feature for other users: legitimate shielded transfers can be made to fail validation/admission (`pushTransaction` returning `false`) or to never be included in a block due to the exclusive one-per-block slot being continuously consumed by attacker-controlled shielded transactions. This is a griefing/denial-of-service vector against a specific API surface (shielded transactions) reachable by any transaction broadcaster, with no privileged role required, and it directly prevents honest users' signed, fee-paying transactions from being processed — the same class of impact (forced revert/exclusion of legitimate user transactions via a permissionless shared lock) identified as High severity in the source report.

### Likelihood Explanation
Likelihood is high given the low barrier to entry: any account can construct and broadcast `ShieldedTransferContract` transactions paying only the standard shielded transaction fee, with no special stake, role, or governance permission needed. The relevant caps (`shieldedTransInPendingMaxCounts` default 10, and the hard-coded per-block cap of 1) are small enough that a modestly funded attacker can keep them saturated continuously, especially against a specific, time-sensitive victim transaction that the attacker can observe in the mempool before it is included.

### Recommendation
Do not rely on a single global counter or a single global per-block slot for shielded transaction admission/inclusion that any anonymous sender can exhaust. Consider: (a) tracking pending shielded-transaction slots per-account rather than globally, (b) increasing/parameterizing the per-block shielded transaction limit and admission cap based on network capacity rather than a small fixed constant, (c) using fee/priority-based selection among competing shielded transactions instead of first-come-first-served exclusive slots, and (d) ensuring the pending-pool counter is reliably decremented (on inclusion, eviction, or expiration) so that the cap reflects only genuinely outstanding transactions and cannot be kept artificially saturated by a single actor.

### Proof of Concept
1. Attacker funds one or more accounts and repeatedly submits self-referential `ShieldedTransferContract` transactions (paying the shielded transaction fee) to the network.
2. Because `pushTransaction` checks only the global `shieldedTransInPendingCounts` against `shieldedTransInPendingMaxCounts` ( [5](#0-4) ), the attacker keeps the pending pool's shielded-transaction slots full, causing a targeted victim's subsequent shielded transaction submission to return `false` (silently rejected) or to remain stuck in the pending pool.
3. Even if the victim's transaction is admitted, block assembly only ever includes one shielded transaction per block ( [4](#0-3) ); the attacker can continue broadcasting new shielded transactions each round so that their own transaction is consistently chosen instead of the victim's, indefinitely delaying or effectively denying the victim's transaction.

### Citations

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L185-185)
```java
  private static final int SHIELDED_TRANS_IN_BLOCK_COUNTS = 1;
```

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L924-943)
```java
        synchronized (this) {
          if (isShieldedTransaction(trx.getInstance())
                  && shieldedTransInPendingCounts.get() >= shieldedTransInPendingMaxCounts) {
            return false;
          }
          if (!session.valid()) {
            session.setValue(revokingStore.buildSession());
          }

          try (ISession tmpSession = revokingStore.buildSession()) {
            processTransaction(trx, null);
            trx.setTrxTrace(null);
            pendingTransactions.add(trx);
            Metrics.gaugeInc(MetricKeys.Gauge.MANAGER_QUEUE, 1,
                    MetricLabels.Gauge.QUEUE_PENDING);
            tmpSession.merge();
          }
          if (isShieldedTransaction(trx.getInstance())) {
            shieldedTransInPendingCounts.incrementAndGet();
          }
```

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L1320-1326)
```java
          if (block.getTransactions().stream()
                  .filter(tran -> isShieldedTransaction(tran.getInstance()))
                  .count() > SHIELDED_TRANS_IN_BLOCK_COUNTS) {
            throw new BadBlockException(
                String.format("num: %d, shielded transaction count > %d",
                    block.getNum(), SHIELDED_TRANS_IN_BLOCK_COUNTS));
          }
```

**File:** common/src/main/resources/reference.conf (L366-368)
```text
  # Shielded transaction (ZK)
  zenTokenId = "000000"
  shieldedTransInPendingMaxCounts = 10 # Max shielded transactions in pending pool.
```
