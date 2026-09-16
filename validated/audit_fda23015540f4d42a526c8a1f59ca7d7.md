### Title
Self-produced block application failure is logged and swallowed instead of halting the node, after the block has already been broadcast - ([File: framework/src/main/java/org/tron/core/consensus/BlockHandleImpl.java])

### Summary
`BlockHandleImpl.produce()` broadcasts a witness-produced block to the network *before* it confirms that the node has successfully applied that same block to its own local chain state. If `manager.pushBlock(blockCapsule)` subsequently throws for any reason, the failure is caught by a blanket `catch (Exception e)` that only logs an error and returns `null` — the node process keeps running with divergent internal state instead of halting, mirroring exactly the bug class fixed upstream in Rollkit (panic instead of log on critical header/apply-block failure).

### Finding Description
`BlockHandleImpl.produce()` performs the following sequence: [1](#0-0) 

1. `manager.generateBlock(...)` builds, executes, and signs the block (`blockCapsule.sign(...)`), returning the signed capsule.
2. `consensus.receiveBlock(blockCapsule)` records it in DPoS bookkeeping.
3. `tronNetService.broadcast(blockMessage)` **immediately broadcasts the signed block to all peers**.
4. Only *after* broadcasting does the node attempt `manager.pushBlock(blockCapsule)`, which re-executes and commits the block into the node's own chain state (`applyBlock` → `processBlock`, in `framework/src/main/java/org/tron/core/db/Manager.java`).

If step 4 throws any exception — including `ValidateScheduleException`, `TaposException`, `TransactionExpirationException`, `DupTransactionException`, `BadBlockException`, `UnLinkedBlockException`, etc., all of which `Manager.pushBlock` can legitimately raise — the exception propagates up through `Manager.pushBlock`'s own `catch (Throwable throwable)` block (which removes the block from `khaosDb` and rethrows): [2](#0-1) 

...and is ultimately caught in `BlockHandleImpl.produce()`, where it is **only logged**, not treated as fatal:
```java
try {
  consensus.receiveBlock(blockCapsule);
  BlockMessage blockMessage = new BlockMessage(blockCapsule);
  tronNetService.broadcast(blockMessage);
  manager.pushBlock(blockCapsule);
} catch (Exception e) {
  logger.error("Handle block {} failed.", blockCapsule.getBlockId().getString(), e);
  return null;
}
``` [3](#0-2) 

Because the block was already broadcast in step 3, peers may receive, independently validate, and accept the block (it carries a valid witness signature and merkle root) even though the producing node itself never committed it to its own `khaosDb`/`DynamicPropertiesStore`. The producing node continues running as if nothing happened — no panic, no halt, no resynchronization trigger — leaving its local view of the chain silently diverged from the network's view of the chain that now includes a block the producer node believes never existed.

This is the same defect class as the upstream Rollkit fix referenced in the report: a critical, should-never-happen failure during a node's own block production/application path (header validation or `applyBlock` failure) was merely logged, allowing the node to keep operating in a corrupted state, instead of panicking to force operator intervention as recommended by the CometBFT halt-on-apply-failure convention referenced in that fix.

### Impact Explanation
A witness (SR) node that hits this failure mode continues operating with local chain state that has fallen behind/diverged from the block it has already broadcast to the rest of the network. This can contribute to a chain split / consensus divergence: the rest of the network builds on the block, while the producer's own node does not have it applied and may attempt to re-produce/re-schedule around the same slot, or silently lag. Because the failure is swallowed rather than causing a fail-fast halt, there is no automatic mechanism forcing the operator to detect and recover from the inconsistency, unlike the intended "halt the node" behavior used elsewhere in `Manager` (see `TronError.ErrCode` panics on `blockTrigger` failure, `framework/src/main/java/org/tron/core/db/Manager.java` lines 1451-1455 for contrast, where similar internal invariant violations do throw `TronError`).

### Likelihood Explanation
Triggering `pushBlock` to fail after a block has already been generated and broadcast requires an ordinary edge condition (e.g., a fork/khaos-db race, a transaction whose validity window elapses between generation and re-application, or any of the many checked exceptions `applyBlock`/`processBlock` can raise) rather than a malicious actor with special privileges — any transaction accepted into the pending pool from an unprivileged broadcaster can be packed into a to-be-produced block and is subject to this window between `generateBlock` and `pushBlock`.

### Recommendation
In `BlockHandleImpl.produce()`, do not broadcast the block before it has been durably applied via `manager.pushBlock()`, and/or replace the blanket `catch (Exception e) { logger.error(...); return null; }` with a panic/halt (e.g., throwing a fatal `TronError` as done elsewhere in `Manager`) when `pushBlock` fails for a self-generated block that was already announced, so an inconsistent internal/network state cannot silently persist.

### Proof of Concept
Not directly reproducible without a running multi-node cluster; the vulnerable code path is deterministic and is shown above: `BlockHandleImpl.produce()` broadcasts before calling `manager.pushBlock()`, and any exception from `pushBlock()` is caught and merely logged rather than causing the node to halt.

### Citations

**File:** framework/src/main/java/org/tron/core/consensus/BlockHandleImpl.java (L45-60)
```java
  public BlockCapsule produce(Miner miner, long blockTime, long timeout) {
    BlockCapsule blockCapsule = manager.generateBlock(miner, blockTime, timeout);
    if (blockCapsule == null) {
      return null;
    }
    try {
      consensus.receiveBlock(blockCapsule);
      BlockMessage blockMessage = new BlockMessage(blockCapsule);
      tronNetService.broadcast(blockMessage);
      manager.pushBlock(blockCapsule);
    } catch (Exception e) {
      logger.error("Handle block {} failed.", blockCapsule.getBlockId().getString(), e);
      return null;
    }
    return blockCapsule;
  }
```

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L1388-1397)
```java
            long oldSolidNum = getDynamicPropertiesStore().getLatestSolidifiedBlockNum();
            try (ISession tmpSession = revokingStore.buildSession()) {
              applyBlock(newBlock, txs);
              tmpSession.commit();
            } catch (Throwable throwable) {
              logger.error(throwable.getMessage(), throwable);
              khaosDb.removeBlk(block.getBlockId());
              clearSolidityContractTriggerCache(block.getNum());
              throw throwable;
            }
```
