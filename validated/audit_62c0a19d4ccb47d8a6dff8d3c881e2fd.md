### Title
Uncontrolled Resource Consumption via Unbounded, Synchronous Contract Trigger Dispatch in Block Application - (File: framework/src/main/java/org/tron/core/db/Manager.java)

### Summary
Similar to the Authorino issue where an unbounded number of user-registered callbacks are executed synchronously by a single service instance during post-authorization processing, java-tron's `Manager` dispatches an unbounded number of smart-contract LOG/event triggers synchronously, in-line, on the single critical block-application path, with no backpressure or capacity check — unlike the sibling trigger types (block/transaction/solidity triggers) which are bounded and drop-on-full.

### Finding Description
When a node has event subscription enabled, every transaction executed inside `processTransaction()`/`processBlock()` calls `postContractTrigger()`, which iterates `trace.getRuntimeResult().getTriggerList()` — the list of `LOG` opcodes emitted by the executed contract — and processes each one **synchronously and unboundedly**, directly in the block-application call stack: [1](#0-0) 

This differs materially from every other trigger type in the same class (block, transaction, solidity triggers), which are all pushed through a bounded `triggerCapsuleQueue.offer(...)` and explicitly dropped with a log message when the queue is full: [2](#0-1) 

`ContractTriggerCapsule.processTrigger()` ultimately calls `EventPluginLoader.postContractEventTrigger`/`postContractLogTrigger`, which — unless a native ZMQ queue is configured — synchronously iterate every registered listener and invoke `listener.handleContractEventTrigger`/`handleContractLogTrigger` inline, with no capacity check performed at call time: [3](#0-2) 

The class does provide an `isBusy()` capacity check based on `MAX_PENDING_SIZE`, but it is only consulted by the unrelated, asynchronous `BlockEventLoad` scheduler: [4](#0-3) 

It is never consulted before `postContractTrigger()` fires from inside `processTransaction()`, so contract-emitted triggers can never be throttled or dropped — they are always forced through synchronously on the node's single block-processing thread: [5](#0-4) 

An unprivileged contract deployer or caller fully controls the number of LOG events emitted (via `LOG0`–`LOG4` opcodes), bounded only by the transaction's energy limit — not by any trigger-count or trigger-size cap enforced by the plugin dispatch code itself, unlike the `triggerCapsuleQueue` used by other trigger types.

### Impact Explanation
Because contract trigger dispatch happens synchronously inside `processTransaction()`/`processBlock()` — the single-threaded core path by which the node applies every block — any slowness on the listener side (a slow plugin backend, a full ZMQ high-water-mark buffer, slow JSON serialization of large log payloads, or a stalled downstream consumer) directly stalls block application for the entire node. Since the dispatch has no bound/backpressure/drop mechanism (in contrast to the analogous queue-based triggers), an attacker who can cheaply cause many LOG emissions per transaction (and many such transactions per block) can degrade or halt the node's ability to keep up with the chain, i.e., a Denial of Service of the node's core block-processing service — the same class of impact as the Authorino advisory (CWE-400/CWE-770, single-instance unbounded callback processing).

### Likelihood Explanation
This requires no special privilege: any account can deploy a `CreateSmartContract` or invoke `TriggerSmartContract` whose bytecode emits a large number of `LOG` events within the energy allowance of a single transaction, and repeat this across many transactions/blocks. It only manifests on nodes that enable event subscription (`eventPluginLoaded`), which is common on exchange/monitoring full nodes and JSON-RPC-enabled nodes that are directly exposed to the traffic causing the triggers, making this a realistically reachable path for an anonymous API client or contract caller.

### Recommendation
Apply the same bounded/backpressure pattern already used for block/transaction/solidity triggers to contract event/log triggers: route `postContractTrigger()` output through a capacity-checked queue (or consult `EventPluginLoader.isBusy()`/enforce a per-block or per-transaction trigger cap) before invoking listeners, and/or move listener invocation off the block-application thread so a slow or unbounded contract-triggered log stream cannot stall consensus/block import.

### Proof of Concept
1. Enable event subscription on a full node (`eventPluginLoaded = true`) with a plugin/listener that is slow to process events (or a full ZMQ send buffer).
2. Deploy a contract whose fallback/entry function loops emitting many `LOG` events (e.g., `LOG2`) up to the per-transaction energy limit.
3. Submit repeated `TriggerSmartContract` calls to this contract across successive blocks.
4. Observe that `Manager.postContractTrigger()` → `EventPluginLoader.postContractEventTrigger/postContractLogTrigger` is invoked synchronously for every emitted log with no drop/backpressure (contrast with the `triggerCapsuleQueue.offer()` "too many triggers... lost" behavior seen for other trigger types), and that a slow listener stalls `processTransaction()`/`processBlock()`, delaying or halting further block application on that node.

### Citations

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L1591-1597)
```java
    // if event subscribe is enabled, post contract triggers to queue
    // only trigger when process block
    if (Objects.nonNull(blockCap) && !blockCap.isMerkleRootEmpty()
        && EventPluginLoader.getInstance().getVersion() == 0) {
      String blockHash = blockCap.getBlockId().toString();
      postContractTrigger(trace, false, blockHash);
    }
```

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L2411-2425)
```java
  private long postTransactionTrigger(final TransactionCapsule trxCap,
      final BlockCapsule blockCap, int index, long preCumulativeEnergyUsed,
      long cumulativeLogCount, final TransactionInfo transactionInfo, long energyUnitPrice,
      boolean removed) {
    TransactionLogTriggerCapsule trx = new TransactionLogTriggerCapsule(trxCap, blockCap,
        index, preCumulativeEnergyUsed, cumulativeLogCount, transactionInfo, energyUnitPrice);
    trx.setLatestSolidifiedBlockNumber(getDynamicPropertiesStore()
        .getLatestSolidifiedBlockNum());
    trx.setRemoved(removed);
    if (!triggerCapsuleQueue.offer(trx)) {
      logger.info("Too many triggers, transaction trigger lost: {}.", trxCap.getTransactionId());
    }

    return trx.getTransactionLogTrigger().getEnergyUsageTotal();
  }
```

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L2494-2513)
```java
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
```

**File:** framework/src/main/java/org/tron/common/logsfilter/EventPluginLoader.java (L536-573)
```java
  public void postSolidityEventTrigger(ContractEventTrigger trigger) {
    if (useNativeQueue) {
      NativeMessageQueue.getInstance()
          .publishTrigger(toJsonString(trigger), trigger.getTriggerName());
    } else {
      eventListeners.forEach(listener ->
          listener.handleSolidityEventTrigger(toJsonString(trigger)));
    }
  }

  public void postTransactionTrigger(TransactionLogTrigger trigger) {
    if (useNativeQueue) {
      NativeMessageQueue.getInstance()
          .publishTrigger(toJsonString(trigger), trigger.getTriggerName());
    } else {
      eventListeners.forEach(listener -> listener.handleTransactionTrigger(toJsonString(trigger)));
    }
  }

  public void postContractLogTrigger(ContractLogTrigger trigger) {
    if (useNativeQueue) {
      NativeMessageQueue.getInstance()
          .publishTrigger(toJsonString(trigger), trigger.getTriggerName());
    } else {
      eventListeners.forEach(listener ->
          listener.handleContractLogTrigger(toJsonString(trigger)));
    }
  }

  public void postContractEventTrigger(ContractEventTrigger trigger) {
    if (useNativeQueue) {
      NativeMessageQueue.getInstance()
          .publishTrigger(toJsonString(trigger), trigger.getTriggerName());
    } else {
      eventListeners.forEach(listener ->
          listener.handleContractEventTrigger(toJsonString(trigger)));
    }
  }
```

**File:** framework/src/main/java/org/tron/core/services/event/BlockEventLoad.java (L37-49)
```java
  public void init() {
    executor.scheduleWithFixedDelay(() -> {
      try {
        if (!instance.isBusy()) {
          load();
        }
      } catch (Exception e) {
        close();
        logger.error("Event load service fail.", e);
      }
    }, 100, 100, TimeUnit.MILLISECONDS);
    logger.info("Event load service start.");
  }
```
