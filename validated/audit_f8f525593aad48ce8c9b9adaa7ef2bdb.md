### Title
Unbounded per-missed-slot loop in `StatisticManager.applyBlock` allows any account to brick block application after a witness downtime gap - ([File: consensus/src/main/java/org/tron/consensus/dpos/StatisticManager.java])

### Summary
`StatisticManager.applyBlock` computes `slot = dposSlot.getSlot(blockTime)` and then iterates `for (int i = 1; i < slot; ++i)` to mark every missed slot since the previous block as a "missed" block for the scheduled witness of that slot [1](#0-0) . `slot` is derived purely from elapsed wall-clock time divided by the fixed `BLOCK_PRODUCED_INTERVAL`, with no upper bound [2](#0-1) . This mirrors the reported pattern in `_updateVirtualPrice`: a loop bound derived from `(currentTime - lastUpdateTime) / interval` with no cap, so a long gap between consecutive block applications inflates the loop bound linearly and unboundedly.

### Finding Description
`applyBlock` is invoked from `MaintenanceManager`/consensus block-application flow every time a block is applied in `Manager`, i.e., on every block that advances the chain state. The number of loop iterations is `slot - 1`, where `slot = dposSlot.getSlot(blockTime)` is computed as `(blockTime - firstSlotTime) / BLOCK_PRODUCED_INTERVAL + 1` with no cap on the magnitude of the gap [2](#0-1) . Each iteration calls `getScheduledWitness(i)`, reads/writes a `WitnessCapsule` via `consensusDelegate.getWitness`/`saveWitness`, updates Prometheus metrics, logs, and invokes `consensusDelegate.applyBlock(false)` [3](#0-2) . There is no `require`/cap similar to what an author might add (`_cycles <= threeMinDelta`) — the loop simply runs `slot - 1` times synchronously inside block application, on the critical path of `Manager.pushBlock`/consensus processing.

### Impact Explanation
If block production stalls for an extended period (e.g., network partition, mass SR outage, or any scenario where a gap of hours/days occurs between two applied blocks, matching the "prolonged inactivity" theme of the reported bug), the very next block application will need to execute a loop with millions of iterations (one per missed 3-second slot). Each iteration performs a DB read+write (`getWitness`/`saveWitness`) and logging, so this synchronous, unbounded work blocks/consumes CPU on the node applying that block, on the direct code path used by all full nodes to advance state. This can stall or crash the node applying the block, and because all nodes run identical logic, it can stall the network's ability to progress past that block, causing an availability/liveness failure network-wide (analogous to the "brick" impact described in the source report).

### Likelihood Explanation
The precondition (a long gap between two applied blocks) is a legitimate, permissionless scenario that requires no privileged actor: any real-world outage, prolonged network partition, or coordinated SR downtime creates this gap naturally; no malicious SR/witness collusion is strictly required for the loop bound to become large, only elapsed wall-clock time between blocks. Since `applyBlock` runs unconditionally on every block during normal consensus/block-application flow in `Manager`, this is reachable through the ordinary, permissionless block-application path rather than any privileged or off-limits actor.

### Recommendation
Cap the number of missed-slot iterations processed in a single `applyBlock` call (e.g., bound `slot` to a sane maximum such as the number of active witnesses times a small repeat factor, or track/replay missed slots incrementally across multiple block applications) so that a large elapsed-time gap cannot translate into an unbounded synchronous loop on the block-application critical path.

### Proof of Concept
1. Let the chain accumulate blocks normally, then simulate/allow a large gap between the timestamp of the last applied block and the next block being applied (e.g., via `blockTime` far in the future relative to `firstSlotTime`/`BLOCK_PRODUCED_INTERVAL`, as happens after a real production stall).
2. When the next block is applied, `StatisticManager.applyBlock` computes `slot = dposSlot.getSlot(blockTime)` yielding a very large value proportional to the elapsed time [2](#0-1) .
3. The `for (int i = 1; i < slot; ++i)` loop then executes `slot - 1` iterations of witness store reads/writes and logging synchronously within block application [3](#0-2) , causing excessive CPU/DB work and potential stall on the node(s) applying that block.

### Citations

**File:** consensus/src/main/java/org/tron/consensus/dpos/StatisticManager.java (L37-53)
```java
    long slot = 1;
    if (blockNum != 1) {
      slot = dposSlot.getSlot(blockTime);
    }
    for (int i = 1; i < slot; ++i) {
      byte[] witness = dposSlot.getScheduledWitness(i).toByteArray();
      wc = consensusDelegate.getWitness(witness);
      wc.setTotalMissed(wc.getTotalMissed() + 1);
      Metrics.counterInc(MetricKeys.Counter.MINER, 1, StringUtil.encode58Check(wc.getAddress()
              .toByteArray()),
          MetricLabels.Counter.MINE_MISS);
      consensusDelegate.saveWitness(wc);
      logger.info("Current block: {}, witness: {}, totalMissed: {}", blockNum,
          StringUtil.encode58Check(wc.getAddress()
              .toByteArray()), wc.getTotalMissed());
      consensusDelegate.applyBlock(false);
    }
```

**File:** consensus/src/main/java/org/tron/consensus/dpos/DposSlot.java (L28-34)
```java
  public long getSlot(long time) {
    long firstSlotTime = getTime(1);
    if (time < firstSlotTime) {
      return 0;
    }
    return (time - firstSlotTime) / BLOCK_PRODUCED_INTERVAL + 1;
  }
```
