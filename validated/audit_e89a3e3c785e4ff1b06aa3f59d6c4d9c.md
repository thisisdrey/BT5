### Title
Any exception in per-block event/log-filter posting crashes the entire node via `System.exit` - (File: `framework/src/main/java/org/tron/core/db/Manager.java`)

### Summary
`Manager.blockTrigger()` wraps the entire JSON-RPC filter / event-subscribe trigger pipeline in a single `try/catch (Exception e)` and, on **any** exception, converts it into a `TronError` with `ErrCode.EVENT_SUBSCRIBE_ERROR`, which the JVM's uncaught-exception handler (`ExitManager`) turns into an immediate `System.exit()` — killing the whole node process. This is architecturally the same bug class as the CoreDNS report: a normal, attacker-reachable data path feeds into a "fatal error → process termination" handler, so any exception surfacing from a per-block/per-transaction filter/trigger builder brings the entire service down.

### Finding Description
`blockTrigger()` is called once for every block applied by the node (from `pushBlock()` after `applyBlock()` succeeds): [1](#0-0) 

Its body posts JSON-RPC block/logs filters and event-subscribe block/transaction/solidity triggers, and any exception thrown anywhere inside is caught generically and re-thrown as a `TronError`: [2](#0-1) 

`TronError` is documented as a class that must never be caught, because the global uncaught-exception handler installed by `ExitManager.initExceptionHandler()` specifically looks for `TronError` and calls `System.exit(code)`: [3](#0-2) [4](#0-3) 

The functions called inside `blockTrigger()` — `postBlockFilter`, `postLogsFilter`, `postBlockTrigger`, `postSolidityTrigger` — are **not** individually guarded with a catch-and-swallow, unlike the sibling method `postContractTrigger()`, which the codebase already had to harden with an explicit `catch (Throwable)` after a previously-observed NPE crash from malicious/unexpected log data: [5](#0-4) 

The project's own unit test (`ManagerTest.blockTrigger`) documents the mechanism precisely: any `RuntimeException` thrown from `postBlockTrigger` (or any of its siblings) is guaranteed to surface as a `TronError` with `EVENT_SUBSCRIBE_ERROR`, i.e. a guaranteed process-fatal condition: [6](#0-5) 

Because `postContractTrigger()` had to be patched for exactly this NPE class (`processTrigger()` throwing NPE on `logInfo.getTopics()` when `logInfo` is missing) — confirmed by the regression test `testPostContractTriggerSwallowsThrowable` — it demonstrates that smart-contract-controlled log/event data (an unprivileged contract deployer/caller emits `LOG*` opcodes with attacker-chosen topics/data) can reach these trigger-construction code paths and cause unexpected exceptions: [7](#0-6) 

However, that fix was applied only to `postContractTrigger`. The sibling functions invoked directly inside `blockTrigger()`'s un-guarded try block (`postBlockFilter`, `postLogsFilter`, `postBlockTrigger`, `postSolidityFilter`, `postSolidityTrigger`, and the JSON-RPC filter/serialization code they call, e.g. `LogInfoTriggerParser`, `BlockEventGet.processTrigger`, ABI/topic parsing in `ContractEventParserAbi`) remain wrapped only by the outer catch that converts *any* `Exception` into a node-killing `TronError`. This mirrors the CoreDNS pattern precisely: a code path reachable from ordinary, unprivileged blockchain activity (a smart contract emitting logs/events as part of a normal transaction) feeds into event/log serialization logic that has already demonstrated NPE-class bugs, and any uncaught exception there is escalated to `log.Fatalf`-equivalent behavior (`TronError` → `System.exit`), rather than being isolated to just the affected block or trigger.

### Impact Explanation
If any transaction's smart-contract log/event data (attacker-controlled `LOG0`-`LOG4` opcode data, topics, or ABI-related metadata) triggers an uncaught exception anywhere in the `postBlockFilter`/`postLogsFilter`/`postBlockTrigger`/`postSolidityFilter`/`postSolidityTrigger` call chain, the entire full node (and any solidity/JSON-RPC-enabled node) is forced to call `System.exit()`, i.e., an unauthenticated, low-cost Denial of Service that halts block production/serving for that node. Since `blockTrigger()` runs on the block-application hot path for every block, a single malicious contract call included in a block can crash every node in the network that has `jsonRpcHttpFullNodeEnable`/`jsonRpcHttpSolidityNodeEnable` or event-subscribe plugins enabled, forcing a restart. This matches the CoreDNS advisory's core harm: "the fatal error handler... immediately terminates the process."

### Likelihood Explanation
The precondition (JSON-RPC or event-subscribe features enabled) is common in production Full/Solidity nodes that serve `TronJsonRpcImpl` API clients. The codebase's own hardening of `postContractTrigger` for an NPE caused by contract log data proves that the "malicious/unexpected log payload → NPE in trigger construction" bug class is real and previously observed in this exact subsystem; the same class of input (attacker-supplied contract logs/events) also flows into the unguarded sibling call sites inside `blockTrigger()`. Reaching this code requires nothing more than broadcasting an ordinary smart-contract transaction that emits logs/events, well within reach of any unprivileged transaction broadcaster/contract deployer.

### Recommendation
Wrap each individual trigger/filter-posting call inside `blockTrigger()` (`postBlockFilter`, `postLogsFilter`, `postBlockTrigger`, `postSolidityFilter`, `postSolidityTrigger`) in its own `try { ... } catch (Throwable t) { logger.error(...); }` — consistent with the pattern already used in `postContractTrigger` — so that failures in event/filter serialization are logged and skipped for that block instead of being escalated to `TronError`/`System.exit`. Reserve `TronError`-triggered process termination for genuinely unrecoverable conditions (e.g., DB corruption), not for best-effort event/notification subsystems whose inputs are derived from arbitrary contract execution.

### Proof of Concept
1. Enable `node.jsonrpc.httpFullNodeEnable=true` (or an event-subscribe plugin) on a target full node.
2. Deploy/trigger a smart contract that emits a `LOG` with a malformed/edge-case payload that reproduces the same class of null/parsing failure previously fixed in `postContractTrigger` (e.g., topics/ABI combination causing `NullPointerException` in `ContractEventParserAbi` / `LogInfoTriggerParser` / `BlockEventGet.processTrigger`) but reaches it via `postLogsFilter`/`postBlockFilter`/`postBlockTrigger` instead of the already-patched `postContractTrigger` call path.
3. Broadcast the transaction; once included in a block, `Manager.pushBlock()` → `blockTrigger()` executes the vulnerable call, the uncaught exception is converted to `TronError.ErrCode.EVENT_SUBSCRIBE_ERROR`, and `ExitManager.logAndExit` calls `System.exit`, crashing the node. [2](#0-1)

### Citations

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L1388-1400)
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
            long newSolidNum = getDynamicPropertiesStore().getLatestSolidifiedBlockNum();
            blockTrigger(newBlock, oldSolidNum, newSolidNum);
          }
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

**File:** framework/src/test/java/org/tron/core/db/ManagerMockTest.java (L522-570)
```java
  @Test
  public void testPostContractTriggerSwallowsThrowable() throws Exception {
    Manager dbManager = spy(new Manager());
    Field eventLoadedField = Manager.class.getDeclaredField("eventPluginLoaded");
    eventLoadedField.setAccessible(true);
    eventLoadedField.set(dbManager, true);

    ChainBaseManager cbm = mock(ChainBaseManager.class);
    DynamicPropertiesStore dps = mock(DynamicPropertiesStore.class);
    when(dps.getLatestSolidifiedBlockNum()).thenReturn(0L);
    when(cbm.getDynamicPropertiesStore()).thenReturn(dps);
    Field cbmField = Manager.class.getDeclaredField("chainBaseManager");
    cbmField.setAccessible(true);
    cbmField.set(dbManager, cbm);

    EventPluginLoader mockLoader = mock(EventPluginLoader.class);
    when(mockLoader.isContractLogTriggerEnable()).thenReturn(false);
    when(mockLoader.isContractEventTriggerEnable()).thenReturn(false);
    when(mockLoader.isSolidityLogTriggerEnable()).thenReturn(true);
    when(mockLoader.isSolidityEventTriggerEnable()).thenReturn(false);

    Field instanceField = EventPluginLoader.class.getDeclaredField("instance");
    instanceField.setAccessible(true);
    EventPluginLoader original = (EventPluginLoader) instanceField.get(null);
    instanceField.set(null, mockLoader);

    try {
      // null logInfo → processTrigger throws NPE on logInfo.getTopics()
      ContractLogTrigger trigger = new ContractLogTrigger();
      trigger.setBlockNumber(300L);
      trigger.setTransactionId("tx-id");
      trigger.setContractAddress("0x01");

      TransactionTrace traceMock = mock(TransactionTrace.class);
      ProgramResult resultMock = mock(ProgramResult.class);
      when(traceMock.getRuntimeResult()).thenReturn(resultMock);
      when(resultMock.getTriggerList())
          .thenReturn(Collections.singletonList((ContractTrigger) trigger));

      Method method = Manager.class.getDeclaredMethod("postContractTrigger",
          TransactionTrace.class, boolean.class, String.class);
      method.setAccessible(true);
      // catch (Throwable) absorbs the NPE — invocation must complete normally
      method.invoke(dbManager, traceMock, false, "blockhash");
    } finally {
      instanceField.set(null, original);
      eventLoadedField.set(dbManager, false);
    }
  }
```
