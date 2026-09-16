### Title
Unrecoverable node crash via `TronError`-wrapped exception in block-trigger post-processing - (File: framework/src/main/java/org/tron/core/db/Manager.java)

### Summary
`Manager.blockTrigger()` wraps *any* exception thrown while posting JSON-RPC filters or event-subscribe triggers for a newly-applied block into a `TronError`, which is an `Error` (not an `Exception`) that the node's global uncaught-exception handler treats as fatal and responds to with `System.exit()`. Because this code runs for every block that is applied — including blocks built from ordinary, attacker-submitted transactions/logs — a transaction crafted to make log/ABI post-processing throw an unexpected `RuntimeException` can take down the whole node, mirroring the free5gc `Npcf_BDTPolicyControl` pattern where a single crafted request produced an unhandled panic that terminated the service.

### Finding Description
`Manager.blockTrigger()` runs after every block is pushed (from `pushBlock`, and also from `switchFork`/`reApplyBlockEvents` paths) and calls, among others, `postLogsFilter`, `postBlockTrigger`, and `postSolidityTrigger`, which build trigger/filter objects (`ContractEventTrigger`, `ContractLogTrigger`, `LogsFilterCapsule`, etc.) out of the log/ABI data produced by executing transactions in that block: [1](#0-0) 

Any `Exception` escaping that block — for example an unexpected `RuntimeException` thrown while parsing an event's ABI/log payload (`ContractEventParserAbi.parseEventData` / `ContractEventParser.parseDataBytes`, or the analogous non-defensive parsing helpers in `BlockEventGet.processTrigger`) — is caught and deliberately re-thrown as:

```java
throw new TronError(e, TronError.ErrCode.EVENT_SUBSCRIBE_ERROR);
```

`TronError` extends `java.lang.Error`, and by design is documented as something that must never be caught: it is meant to trigger a full process exit via the JVM's default uncaught-exception handler: [2](#0-1) [3](#0-2) 

`blockTrigger()` is invoked deep inside `Manager.pushBlock()`'s block-application path, itself reached from the p2p sync/consensus flow (`TronNetDelegate.processBlock` → `dbManager.pushBlock(block)`), which only catches a fixed list of *checked* exceptions (`ValidateSignatureException`, `ContractValidateException`, etc.) — none of which is `Error`/`TronError`: [4](#0-3) 

Because `TronError` is an `Error`, it is not caught by any of those handlers and propagates up the calling thread, ultimately reaching the JVM's default uncaught-exception handler installed by `ExitManager.initExceptionHandler()`, which calls `System.exit(EVENT_SUBSCRIBE_ERROR.getCode())`.

This is architecturally the same bug class as the free5gc advisory: a request-triggered (here, transaction/log-triggered) code path that is supposed to only affect a single request/flow instead escalates to an unrecoverable process-level failure because the "fatal error" escape hatch (`TronError`/panic) is reachable from externally-influenced input rather than being reserved for genuine unrecoverable infrastructure failures (DB corruption, config errors, etc.).

### Impact Explanation
If this code path is triggered on any full node running with `--event-subscribe` (`eventPluginLoaded`), `jsonRpcHttpFullNodeEnable`, or `jsonRpcHttpSolidityNodeEnable` — all common production configurations for full nodes/gateways/exchanges/indexers — a single malformed transaction that causes an unexpected exception during log/event post-processing (as opposed to during TVM execution, which is separately sandboxed and doesn't reach this code) can crash the node process entirely. Because block application, and therefore `blockTrigger()`, is exercised on every node that has synced or produced the block containing the offending transaction, this is a network-wide/relay-wide denial of service, not a single-node curiosity — any full node processing the same chain data hits the identical crash. This satisfies the "node crash or halt" bar for a Medium/High-impact analog.

### Likelihood Explanation
Likelihood is moderate-to-uncertain because I could not conclusively identify, in the code available through search, an unguarded exception in the specific data path (`ContractEventParserAbi.parseTopics`/`parseEventData`, `BlockEventGet.processTrigger`, `LogsFilterCapsule`/`ContractTriggerCapsule` construction) that is reachable with fully attacker-controlled log/topic/ABI content and is not already caught defensively — the repository shows heavy, recent hardening elsewhere (bounded recursion in `JsonFormat`/`JsonRpcServlet`, explicit `ArithmeticException`/`OutputLengthException` catches in `ContractEventParser`, per-precompile stack-overflow guards in the TVM). The clearly-verified, exploitable root cause is architectural: `blockTrigger()` converts *any* `Exception` from this post-processing stage into a fatal, uncatchable `TronError` that kills the node, regardless of whether the underlying exception was itself a genuine infrastructure failure or a transient parsing bug triggered by unusual-but-not-necessarily malicious block content. Given the extensive defensive coding observed elsewhere in this codebase, it's plausible remaining edge cases in log/ABI parsing (e.g., malformed dynamic `ABI.Entry` shapes, adversarial precompile-emitted internal-transaction logs, or overflow conditions in cumulative counters) could still slip through this catch-all and hit the fatal path, but I was not able to fully verify a concrete unguarded throw site within my remaining search budget.

### Recommendation
- Do not translate exceptions from `postBlockFilter`/`postLogsFilter`/`postBlockTrigger`/`postSolidityTrigger` (i.e., any exception whose root cause originates from parsing/serializing transaction or log data derived from a specific block) into `TronError`/process-fatal errors. Reserve `TronError` strictly for infrastructure failures (DB, storage, config) that are not influenced by transaction content.
- Make all trigger/filter construction code defensive against malformed or unexpected ABI/log shapes (mirroring the existing `ContractEventParser` `OutputLengthException`/`ArithmeticException` handling) so that a single bad event cannot escape as an unchecked `RuntimeException`.
- If a genuinely unexpected exception occurs while posting triggers for one block, log it, skip/drop that block's event-subscribe triggers, and continue block application rather than terminating the process — event-subscribe/JSON-RPC filter delivery is an auxiliary feature and its failure should never affect chain consensus/availability.

### Proof of Concept
Not independently reproducible from static analysis alone within this session. The concrete PoC would require constructing a transaction whose smart-contract execution emits a `LOG` opcode with a topic/data shape that, when the block is applied and `Manager.blockTrigger()` → `postLogsFilter`/`postBlockTrigger` → `ContractEventParserAbi.parseEventData`/`BlockEventGet.processTrigger` run over it (with `--event-subscribe` or JSON-RPC filters enabled), throws an uncaught `RuntimeException` (e.g., a corner case in dynamic-type offset/length decoding not covered by the existing `OutputLengthException`/`ArithmeticException` catches). Once that exception surfaces, `blockTrigger()` unconditionally converts it into `TronError(EVENT_SUBSCRIBE_ERROR)`, which `ExitManager`'s uncaught-exception handler turns into `System.exit(1)`, crashing the node. Verifying the exact input that reaches an unguarded throw site would require dynamic testing/fuzzing of `ContractEventParserAbi` and `BlockEventGet` against a running node, which is out of scope for static code review.

### Citations

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

**File:** common/src/main/java/org/tron/core/exception/TronError.java (L1-18)
```java
package org.tron.core.exception;

import lombok.Getter;

/**
 * If a {@link TronError} is thrown, the service will trigger {@link System#exit(int)} by
 * {@link Thread#setDefaultUncaughtExceptionHandler(Thread.UncaughtExceptionHandler)}.
 * NOTE: Do not attempt to catch {@link TronError}.
 */
@Getter
public class TronError extends Error {

  private final ErrCode errCode;

  public TronError(String message, ErrCode exitCode) {
    super(message);
    this.errCode = exitCode;
  }
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

**File:** framework/src/main/java/org/tron/core/net/TronNetDelegate.java (L294-321)
```java
        }
      } catch (ValidateSignatureException
          | ContractValidateException
          | ContractExeException
          | UnLinkedBlockException
          | ValidateScheduleException
          | AccountResourceInsufficientException
          | TaposException
          | TooBigTransactionException
          | TooBigTransactionResultException
          | DupTransactionException
          | TransactionExpirationException
          | BadNumberBlockException
          | BadBlockException
          | NonCommonBlockException
          | ReceiptCheckErrException
          | VMIllegalException
          | ZksnarkException
          | EventBloomException e) {
        metricsService.failProcessBlock(block.getNum(), e.getMessage());
        logger.error("Process block failed, {}, reason: {}", blockId.getString(), e.getMessage());
        if (e instanceof BadBlockException
                && ((BadBlockException) e).getType().equals(CALC_MERKLE_ROOT_FAILED)) {
          throw new P2pException(TypeEnum.BLOCK_MERKLE_INVALID, e);
        } else {
          throw new P2pException(TypeEnum.BAD_BLOCK, e);
        }
      }
```
