### Title
Manager.blockTrigger() Converts Any Exception in the Event-Trigger Pipeline into an Unrecoverable `System.exit()`, Allowing a Single Transaction to Halt the Node - (File: `framework/src/main/java/org/tron/core/db/Manager.java`)

### Summary
The reported Solana-poller issue is that unhandled `panic()` calls in error-handling paths can abort the whole bridge process instead of just failing the offending unit of work, with no panic-recovery in place. The equivalent Java analog exists in `Manager.blockTrigger()`, which is invoked synchronously for **every** applied block (self-produced or received) as part of `pushBlock`/`applyBlock`. Unlike the other background-loop `Runnable`s in the same class (`triggerCapsuleProcessLoop`, `filterProcessLoop`, `postContractTrigger`) which deliberately `catch (Throwable)` and merely log the failure so the node keeps running, `blockTrigger()` wraps *any* `Exception` thrown while posting JSON-RPC filters or event-subscribe triggers into a `TronError`, which by design is never supposed to be caught and is picked up by the global `Thread.setDefaultUncaughtExceptionHandler` to call `System.exit(code)` — i.e. a full, unrecoverable node halt.

### Finding Description
`Manager.blockTrigger()` runs on the block-application hot path: [1](#0-0) 

Any `Exception` raised while building/posting `BlockFilterCapsule`, `LogsFilterCapsule`, `BlockLogTriggerCapsule`, `TransactionLogTriggerCapsule`, or contract event/log triggers for the just-applied block is escalated to a `TronError(e, ErrCode.EVENT_SUBSCRIBE_ERROR)`. `TronError` extends `Error` and is explicitly documented as something that must never be caught, because the global uncaught-exception handler installed by `ExitManager` treats any `TronError` as fatal and calls `System.exit`: [2](#0-1) [3](#0-2) 

Contrast this with the sibling background loops in the same `Manager` class, all of which intentionally swallow `Throwable` to keep the subsystem alive: [4](#0-3) [5](#0-4) 

`blockTrigger()` is called from `pushBlock` right after every block (whether self-generated or received from the network/synced) is applied, via `applyBlock` → `pushBlock`, so it executes for blocks that include arbitrary user-submitted, unprivileged transactions (e.g. `TriggerSmartContract` calls that emit logs/events). The test suite confirms this behavior is by design: a single exception thrown deep in the trigger-posting call chain (`postBlockTrigger`, which recursively calls `processTransactionTrigger`/`postTransactionTrigger` for every transaction in the block, including logs decoded from user-controlled contract data) is converted into a fatal `TronError` that must propagate to `System.exit`: [6](#0-5) 

### Impact Explanation
Because `postLogsFilter`/`postBlockTrigger`/`processTransactionTrigger` operate on transaction logs and `TransactionInfo` derived from block contents, any bug (NPE, ArrayIndexOutOfBounds, unexpected null field, malformed contract-event decoding, etc.) triggered by a specially crafted, otherwise-valid transaction (e.g. a smart contract emitting logs with unusual data/topics, or a mismatch between transaction/receipt counts) in the trigger-posting code path is escalated from a local failure into a full-node crash via `System.exit`. Any full node that has JSON-RPC filters (`isJsonRpcHttpFullNodeEnable`/`isJsonRpcHttpSolidityNodeEnable`) or event subscription (`Args.getInstance().isEventSubscribe()`) enabled — a common production configuration for indexers, exchanges, and infra providers — would go offline, unable to serve API requests or continue producing/validating blocks, until manually restarted. This is a node-halt / availability (DoS) issue reachable by any unprivileged party who can broadcast a transaction whose logs/events exercise a latent bug in the trigger pipeline, unlike the deliberately resilient parallel background loops in the same class.

### Likelihood Explanation
Likelihood is elevated because `blockTrigger()` executes unconditionally on the normal block-application path for every block containing user transactions, and its exception handling is unconditionally fail-fast (any `Exception`, not a narrow allow-list, becomes fatal), in contrast to every other loop in `Manager` that was hardened to swallow `Throwable`. The finding is validated by the existing unit test `ManagerTest.blockTrigger()`, which demonstrates that an arbitrary `RuntimeException` thrown inside the trigger-posting call graph is converted to `TronError`/`EVENT_SUBSCRIBE_ERROR`, the exact behavior that leads to `System.exit`.

### Recommendation
Bring `blockTrigger()`'s error handling in line with the other trigger-processing loops in `Manager` (`triggerCapsuleProcessLoop`, `filterProcessLoop`, `postContractTrigger`): catch and log failures from JSON-RPC filter posting and event-subscribe trigger posting without escalating to a fatal `TronError`/`System.exit`, reserving `TronError` for genuinely unrecoverable conditions (e.g. storage corruption), not for per-block, user-triggerable event/log serialization failures. At minimum, isolate each trigger-posting step (`postBlockFilter`, `postLogsFilter`, `postSolidityFilter`, `postBlockTrigger`, `postSolidityTrigger`) with its own try/catch so a bug in one plugin/trigger type cannot halt block application for the whole node.

### Proof of Concept
1. Enable event subscription or JSON-RPC filters on a full node.
2. Broadcast/deploy a smart contract whose `TriggerSmartContract` call emits event log data engineered to trip a latent null-pointer/parsing bug anywhere in the trigger construction chain reachable from `postBlockTrigger`/`processTransactionTrigger`/`postTransactionTrigger` (analogous to the NPE already demonstrated being swallowed in `postContractTrigger`'s test, but here occurring in the code path that is *not* wrapped defensively).
3. When the block containing this transaction is applied via `pushBlock` → `applyBlock` → `blockTrigger`, the exception is wrapped into `TronError(EVENT_SUBSCRIBE_ERROR)` as shown in `ManagerTest.blockTrigger()`.
4. `ExitManager`'s uncaught-exception handler detects the `TronError` and calls `System.exit`, terminating the node process — confirmed reachable from ordinary block/transaction processing, not from a privileged or internal-only code path.

### Citations

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L320-335)
```java
  private Runnable triggerCapsuleProcessLoop =
      () -> {
        while (isRunTriggerCapsuleProcessThread) {
          try {
            TriggerCapsule triggerCapsule = triggerCapsuleQueue.poll(1, TimeUnit.SECONDS);
            if (triggerCapsule != null) {
              triggerCapsule.processTrigger();
            }
          } catch (InterruptedException ex) {
            logger.info(ex.getMessage());
            Thread.currentThread().interrupt();
          } catch (Throwable throwable) {
            logger.error("Unknown throwable happened in process capsule loop.", throwable);
          }
        }
      };
```

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L1429-1456)
```java
  void blockTrigger(final BlockCapsule block, long oldSolid, long newSolid) {
    // post block and logs for jsonrpc
    try {
      if (CommonParameter.getInstance().isJsonRpcHttpFullNodeEnable()) {
        postBlockFilter(block, false);
        postLogsFilter(block, false, false);
      }

      if (CommonParameter.getInstance().isJsonRpcHttpSolidityNodeEnable()) {
        postSolidityFilter(oldSolid, newSolid);
      }

      if (EventPluginLoader.getInstance().getVersion() != 0) {
        lastUsedSolidityNum = newSolid;
        return;
      }

      // if event subscribe is enabled, post block trigger to queue (real-time, not removed)
      postBlockTrigger(block, false);
      // if event subscribe is enabled, post solidity trigger to queue
      // (also emits solidified-mode block/transaction triggers)
      postSolidityTrigger(newSolid);
    } catch (Exception e) {
      logger.error("Block trigger failed. head: {}, oldSolid: {}, newSolid: {}",
          block.getNum(), oldSolid, newSolid, e);
      throw new TronError(e, TronError.ErrCode.EVENT_SUBSCRIBE_ERROR);
    }
  }
```

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L2487-2515)
```java
  private void postContractTrigger(final TransactionTrace trace, boolean remove, String blockHash) {
    boolean isContractTriggerEnable = EventPluginLoader.getInstance()
        .isContractEventTriggerEnable() || EventPluginLoader
        .getInstance().isContractLogTriggerEnable();
    boolean isSolidityContractTriggerEnable = EventPluginLoader.getInstance()
        .isSolidityEventTriggerEnable() || EventPluginLoader
        .getInstance().isSolidityLogTriggerEnable();
    if (eventPluginLoaded
        && (isContractTriggerEnable || isSolidityContractTriggerEnable)) {
      // be careful, trace.getRuntimeResult().getTriggerList() should never return null
      for (ContractTrigger trigger : trace.getRuntimeResult().getTriggerList()) {
        ContractTriggerCapsule contractTriggerCapsule = new ContractTriggerCapsule(trigger);
        contractTriggerCapsule.getContractTrigger().setRemoved(remove);
        contractTriggerCapsule.setLatestSolidifiedBlockNumber(getDynamicPropertiesStore()
            .getLatestSolidifiedBlockNum());
        contractTriggerCapsule.setBlockHash(blockHash);

        // Process synchronously to avoid race condition between async queue and
        // reOrgContractTrigger cache clearing. Performance is not impacted because
        // processTrigger() only enqueues events into the plugin's internal queue
        // without blocking on actual I/O.
        try {
          contractTriggerCapsule.processTrigger();
        } catch (Throwable throwable) {
          logger.warn("Post contract trigger failed.", throwable);
        }
      }
    }
  }
```

**File:** common/src/main/java/org/tron/core/exception/TronError.java (L1-9)
```java
package org.tron.core.exception;

import lombok.Getter;

/**
 * If a {@link TronError} is thrown, the service will trigger {@link System#exit(int)} by
 * {@link Thread#setDefaultUncaughtExceptionHandler(Thread.UncaughtExceptionHandler)}.
 * NOTE: Do not attempt to catch {@link TronError}.
 */
```

**File:** common/src/main/java/org/tron/common/exit/ExitManager.java (L23-52)
```java
  public static void initExceptionHandler() {
    Thread.setDefaultUncaughtExceptionHandler((t, e) -> {
      findTronError(e).ifPresent(ExitManager::logAndExit);
      logger.error("Uncaught exception", e);
    });
  }

  public static Optional<TronError> findTronError(Throwable e) {
    if (e == null) {
      return Optional.empty();
    }

    Set<Throwable> seen = new HashSet<>();

    while (e != null && !seen.contains(e)) {
      if (e instanceof TronError) {
        return Optional.of((TronError) e);
      }
      seen.add(e);
      e = e.getCause();
    }
    return Optional.empty();
  }

  public static void logAndExit(TronError exit) {
    final int code = exit.getErrCode().getCode();
    logger.error("Shutting down with code: {}, reason: {}", exit.getErrCode(), exit.getMessage());
    Thread exitThread = exitThreadFactory.newThread(() -> System.exit(code));
    exitThread.start();
  }
```

**File:** framework/src/test/java/org/tron/core/db/ManagerTest.java (L1387-1395)
```java
  @Test
  public void blockTrigger() {
    Manager manager = spy(new Manager());
    doThrow(new RuntimeException("postBlockTrigger mock")).when(manager)
        .postBlockTrigger(any(), anyBoolean());
    TronError thrown = Assert.assertThrows(TronError.class, () ->
        manager.blockTrigger(new BlockCapsule(Block.newBuilder().build()), 1, 1));
    Assert.assertEquals(TronError.ErrCode.EVENT_SUBSCRIBE_ERROR, thrown.getErrCode());
  }
```
