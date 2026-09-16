### Title
Silent exception swallowing during fork switchback in `Manager.switchFork` can corrupt chain state instead of halting the node - (File: `framework/src/main/java/org/tron/core/db/Manager.java`)

### Summary
The CometBFT fix addresses a case where the mempool continued running after an ABCI application returned an unrecoverable error on `CheckTx`, instead of stopping the node — because continuing execution after such an error can silently corrupt consensus state. java-tron's block-application path in `Manager.switchFork` has the same anti-pattern: when re-applying the original ("losing") branch of blocks after a failed fork switch, any of a wide set of block-application exceptions are caught, logged, and swallowed, allowing the node to keep running with a partially-applied chain state rather than halting.

### Finding Description
`Manager.switchFork` applies a new branch of blocks in a loop [1](#0-0) . If applying any block in the new branch throws one of the listed exceptions, the code logs it, sets `exception`, and rethrows — this looks correct at first glance. But the `finally` block then attempts to roll back to the old head and "switch back" by re-applying the original branch (`second`) of blocks [2](#0-1) .

In that switchback re-application loop, if `applyBlock(khaosBlock.getBlk().setSwitch(true))` throws `AccountResourceInsufficientException`, `ValidateSignatureException`, `ContractValidateException`, `ContractExeException`, `TaposException`, `DupTransactionException`, `TransactionExpirationException`, `TooBigTransactionException`, `ValidateScheduleException`, or `ZksnarkException`, the exception is only logged via `logger.warn(e.getMessage(), e)` — it is neither rethrown nor does the loop abort [3](#0-2) . Execution simply proceeds to the next `khaosBlock` in `second`, leaving `chainBaseManager`'s dynamic properties (`latestBlockHeaderHash`/Number), `blockStore`, and derived state (account balances, energy, votes, etc.) reflecting only a partial re-application of the original canonical chain. The node does not halt, panic, or resync — it continues to accept new transactions and blocks (`pushTransaction`, `broadcastTransaction`, HTTP/gRPC APIs) on top of this inconsistent state.

This mirrors exactly the bug class fixed in the cited CometBFT commit: an unrecoverable/critical error during transitional state application is caught and the node is allowed to continue operating on divergent or corrupted state instead of stopping.

### Impact Explanation
A node that hits this swallowed-exception path during switchback ends up with a chain state that no longer matches the state produced by correctly applying the canonical (`second`) branch block-by-block. Because the loop already committed some blocks via `tmpSession.commit()` before the failure and then simply skips the rest, the node's `latestBlockHeaderHash`/`Number` and account/witness/asset state can diverge from the network's consensus state while the node keeps operating normally. This can lead to: incorrect balances being served over APIs, incorrect signature/permission checks against stale state, and ultimately a chain split for that node (it will reject or misprocess future blocks/transactions that peers consider valid), a permanent and hard-to-detect state corruption.

### Likelihood Explanation
Triggering `switchFork`'s failure/switchback path deterministically from a single crafted transaction is difficult on its own since fork resolution is driven by block production/reception. However, the underlying defect — catching a broad list of validation/execution exceptions during the *reapplication of already-canonical blocks* and merely logging them without halting or aborting — is a structural correctness bug independent of the trigger mechanism, and it is explicitly present in code that performs "block application in Manager," which is in scope for this analysis. Any condition that causes `applyBlock` to throw during switchback (e.g., a resource-insufficient or expiration exception surfaced by re-validating a transaction under altered account state) reaches this silent-swallow path.

### Recommendation
In the switchback loop of `switchFork`, do not silently swallow exceptions from `applyBlock`. Any exception at this stage indicates the node cannot reliably restore the previously-canonical branch; the safe behavior (matching the "stop rather than continue on unrecoverable app error" principle from the referenced fix) is to treat this as fatal — abort further block processing and surface/panic (e.g., throw a `TronError`/`RuntimeException` that stops the node) rather than logging and proceeding to the next block. At minimum, the loop should stop iterating over `second` as soon as an exception occurs and flag the node as requiring manual intervention/resync instead of continuing to serve traffic on top of partially-applied state.

### Proof of Concept
Conceptual reproduction (cannot be fully deterministic from a single transaction without also controlling block production, but demonstrates the code defect):
1. Cause a fork switch attempt where the new branch (`first`) fails partway through `applyBlock`, entering the `finally` switchback path.
2. During re-application of the original branch (`second`), craft conditions (e.g., an account that becomes resource-insufficient or a transaction that now appears expired due to elapsed wall-clock time during the fork-resolution window) so that `applyBlock` throws one of the caught exceptions for a block in `second`.
3. Observe that `Manager.switchFork` logs the exception at line 1206 and returns normally from `finally`, without rethrowing or halting — subsequent code (`pushBlock`) proceeds to log `SAVE_BLOCK` and return as if push succeeded, while `second`'s remaining blocks were never re-applied, leaving dynamic properties inconsistent with the actual data committed to the block/account stores.

### Citations

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L1137-1210)
```java
    if (CollectionUtils.isNotEmpty(binaryTree.getKey())) {
      List<KhaosBlock> first = new ArrayList<>(binaryTree.getKey());
      Collections.reverse(first);
      for (KhaosBlock item : first) {
        Exception exception = null;
        // todo  process the exception carefully later
        try (ISession tmpSession = revokingStore.buildSession()) {
          if (!item.getBlk().validateSignature(
              getDynamicPropertiesStore(), getAccountStore())) {
            throw new ValidateSignatureException(
                "switch fork: block " + item.getBlk().getNum() + " signature invalid");
          }
          // The new branch is applied on a rewound, diverged state where account permissions
          // may have changed, so a cached signature-verification result is no longer
          // trustworthy. Clear it to force every transaction to re-validate its signature
          // against the fork-chain state.
          for (TransactionCapsule tx : item.getBlk().getTransactions()) {
            tx.setVerified(false);
          }
          applyBlock(item.getBlk().setSwitch(true));
          tmpSession.commit();
        } catch (AccountResourceInsufficientException
            | ValidateSignatureException
            | ContractValidateException
            | ContractExeException
            | TaposException
            | DupTransactionException
            | TransactionExpirationException
            | ReceiptCheckErrException
            | TooBigTransactionException
            | TooBigTransactionResultException
            | ValidateScheduleException
            | VMIllegalException
            | ZksnarkException
            | BadBlockException e) {
          logger.warn(e.getMessage(), e);
          exception = e;
          throw e;
        } finally {
          if (exception != null) {
            Metrics.counterInc(MetricKeys.Counter.BLOCK_FORK, 1, MetricLabels.FAIL);
            MetricsUtil.meterMark(MetricsKey.BLOCKCHAIN_FAIL_FORK_COUNT);
            logger.warn("Switch back because exception thrown while switching forks.", exception);
            first.forEach(khaosBlock -> khaosDb.removeBlk(khaosBlock.getBlk().getBlockId()));
            khaosDb.setHead(binaryTree.getValue().peekFirst());

            while (!getDynamicPropertiesStore()
                .getLatestBlockHeaderHash()
                .equals(binaryTree.getValue().peekLast().getParentHash())) {
              eraseBlock();
            }

            List<KhaosBlock> second = new ArrayList<>(binaryTree.getValue());
            Collections.reverse(second);
            for (KhaosBlock khaosBlock : second) {
              // todo  process the exception carefully later
              try (ISession tmpSession = revokingStore.buildSession()) {
                applyBlock(khaosBlock.getBlk().setSwitch(true));
                tmpSession.commit();
              } catch (AccountResourceInsufficientException
                  | ValidateSignatureException
                  | ContractValidateException
                  | ContractExeException
                  | TaposException
                  | DupTransactionException
                  | TransactionExpirationException
                  | TooBigTransactionException
                  | ValidateScheduleException
                  | ZksnarkException e) {
                logger.warn(e.getMessage(), e);
              }
            }
          }
        }
```
