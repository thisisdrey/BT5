## Analysis

I found a plausible analog to CVE‑2015‑8615's unbounded-logging pattern in the TVM interpreter's exception handling. The Xen bug was about a locally-triggerable, attacker-controlled event causing unlimited kernel log writes with no throttling; the closest reachable equivalent in java-tron is the catch-all exception logging inside the VM opcode dispatch loop, which is executed on every `TriggerSmartContract`/`CreateSmartContract` transaction and unconditionally writes full stack traces without any rate limiting, deduplication, or sampling.

### Title
Unthrottled WARN/INFO stack-trace logging on every TVM interpreter exception enables log-flood DoS - (File: actuator/src/main/java/org/tron/core/vm/VM.java)

### Summary
`VM.play()` is the core TVM opcode dispatch loop invoked for every smart-contract call/creation transaction. Its outer exception handlers unconditionally emit a full stack trace to the log on each "unexpected" `RuntimeException` and on every JVM `StackOverflowError`, without any throttling, counter, or sampling — mirroring the root cause of CVE‑2015‑8615 (unbounded `printk` calls triggered by an unprivileged/low-trust actor).

### Finding Description
In `VM.play()`: [1](#0-0) 

the loop catches generic `RuntimeException`s that escape opcode execution. When the exception is an `NullPointerException` or has an empty message, it calls:
```java
logger.warn("Unknown Exception occurred, tx id: {}",
    Hex.toHexString(program.getRootTransactionId()), e);
```
Passing `e` as the last varargs argument to SLF4J causes the entire exception stack trace to be serialized and written on every occurrence — there is no counter, no per-tx-id/per-window suppression, and no cap on how many times this can be logged. Similarly, the `StackOverflowError` branch unconditionally logs with a full trace at INFO level: [2](#0-1) 

`VM.play()` is invoked from `VMActuator.execute()` for both top-level calls and every nested internal call: [3](#0-2) 

`VMActuator` itself is directly reachable from any signed `TriggerSmartContract`/`CreateSmartContract` transaction via `RuntimeImpl.execute()`, which is invoked from `Manager.processTransaction()` during both `pushTransaction` (mempool) and `processBlock` (consensus block application) — meaning the same unthrottled logging happens on every full node and witness that processes the transaction, not just the originating node: [4](#0-3) [5](#0-4) 

The logback config routes the "VM" logger topic to an `AsyncAppender` with a bounded 100-entry queue that **blocks the calling thread** once full: [6](#0-5) 

This means a sustained flood of log-triggering transactions can not only fill disk with repeated large stack traces but also stall the thread performing block/transaction processing once the async queue saturates — directly analogous to the Xen advisory's console-message flood causing a DoS on the hypervisor.

### Impact Explanation
If a smart-contract call can be crafted to reliably reach this "unknown exception" branch (any deep interpreter bug throwing an unmessaged `RuntimeException`, or a crafted call chain producing a JVM `StackOverflowError`), an attacker can broadcast a stream of cheap, repeatable transactions that each force every processing node (all witnesses/SRs and full nodes) to write a full stack trace to disk with no rate limit. Sustained abuse can exhaust local disk space or block the shared async log-queue thread, degrading or halting block processing network-wide — a legitimate node-crash/halt-class DoS, not merely a cosmetic log nuisance.

### Likelihood Explanation
Reaching this catch-all branch requires an actual internal bug (NPE / unmessaged RuntimeException, or genuine JVM stack overflow) to be triggerable deterministically by attacker-supplied bytecode/calldata. The TVM has a long history of such edge-case bugs (explicitly acknowledged by the JEP-358/JDK-8220715 comment in the code itself), so the mechanism is plausible, but exploitability depends on finding/using a concrete triggering bytecode sequence, which is not proven here — this is a defensive/catch-all path, not a confirmed exploit primitive.

### Recommendation
- Rate-limit or de-duplicate WARN/ERROR-level exception logging in `VM.play()` (e.g., log full stack trace only on first occurrence per interval, then a counter), similar to fixes applied elsewhere in the codebase such as `TransactionCapsule.logSlowSigVerify()`'s threshold gate.
- Avoid logging full stack traces for anticipated/attacker-reachable code paths in the hot execution loop; log a truncated summary instead and reserve full traces for a sampled/throttled diagnostic channel.
- Consider making the VM `AsyncAppender` queue non-blocking (drop oldest) under load rather than blocking the block-processing thread.

### Proof of Concept
Not conclusively demonstrable without a concrete bytecode sequence proven to deterministically throw an unmessaged `RuntimeException` or provoke `StackOverflowError` inside `op.execute(program)`; conceptually: repeatedly broadcast `TriggerSmartContract` transactions against a contract engineered to hit such a path, observing unthrottled stack-trace log growth on every processing node per transaction.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/VM.java (L110-126)
```java
    } catch (JVMStackOverFlowException | OutOfTimeException e) {
      throw e;
    } catch (RuntimeException e) {
      // https://openjdk.org/jeps/358
      // https://bugs.openjdk.org/browse/JDK-8220715
      // since jdk 14, the NullPointerExceptions message is not empty
      if (e instanceof NullPointerException || StringUtils.isEmpty(e.getMessage())) {
        logger.warn("Unknown Exception occurred, tx id: {}",
            Hex.toHexString(program.getRootTransactionId()), e);
        program.setRuntimeFailure(new RuntimeException("Unknown Exception"));
      } else {
        program.setRuntimeFailure(e);
      }
    } catch (StackOverflowError soe) {
      logger.info("\n !!! StackOverflowError: update your java run command with -Xss !!!\n", soe);
      throw new JVMStackOverFlowException();
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/VMActuator.java (L194-196)
```java
        // Prepare the table once for this execution and all nested calls.
        VM.play(program, OperationRegistry.prepareAndGetTable(isConstantCall));
        result = program.getResult();
```

**File:** framework/src/main/java/org/tron/common/runtime/RuntimeImpl.java (L37-54)
```java
  @Override
  public void execute(TransactionContext context)
      throws ContractValidateException, ContractExeException {
    this.context = context;

    ContractType contractType = context.getTrxCap().getInstance().getRawData().getContract(0)
        .getType();
    switch (contractType.getNumber()) {
      case ContractType.TriggerSmartContract_VALUE:
      case ContractType.CreateSmartContract_VALUE:
        actuator2 = new VMActuator(context.isStatic());
        break;
      default:
        actuatorList = ActuatorCreator.getINSTANCE().createActuator(context.getTrxCap());
    }
    if (actuator2 != null) {
      actuator2.validate(context);
      actuator2.execute(context);
```

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L1884-1897)
```java
      for (TransactionCapsule transactionCapsule : block.getTransactions()) {
        rejectExchangeTransaction(transactionCapsule.getInstance());
        if (chainBaseManager.getDynamicPropertiesStore().allowConsensusLogicOptimization()
            && transactionCapsule.retCountIsGreatThanContractCount()) {
          throw new BadBlockException(String.format("The result count %d of this transaction %s is "
                  + "greater than its contract count %d", transactionCapsule.getRetCount(),
              transactionCapsule.getTransactionId(), transactionCapsule.getContractCount()));
        }
        transactionCapsule.setBlockNum(num);
        if (block.generatedByMyself) {
          transactionCapsule.setVerified(true);
        }
        accountStateCallBack.preExeTrans();
        TransactionInfo result = processTransaction(transactionCapsule, block);
```

**File:** framework/src/main/resources/logback.xml (L91-98)
```text
    <!-- Default is 256 -->
    <!-- Logger will block incoming events (log calls) until queue will free some space -->
    <!-- (the smaller value -> flush occurs often) -->
    <queueSize>100</queueSize>
    <includeCallerData>true</includeCallerData>
    <!-- Allow up to 5 s to drain the queue on shutdown before giving up -->
    <maxFlushTime>5000</maxFlushTime>
    <appender-ref ref="FILE"/>
```
