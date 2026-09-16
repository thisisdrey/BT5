### Title
Uncaught NullPointerException in `ParticipateAssetIssueActuator.execute()` can crash block application - (File: `actuator/src/main/java/org/tron/core/actuator/ParticipateAssetIssueActuator.java`)

### Summary
`ParticipateAssetIssueActuator.execute()` fetches an `AssetIssueCapsule` from the store and immediately dereferences it (`assetIssueCapsule.getNum()`) without a null check, while its `catch` clause only covers `InvalidProtocolBufferException | ArithmeticException` [1](#0-0) . If the asset lookup returns `null` at execute-time, a `NullPointerException` is thrown that is not caught by the actuator, not declared by `execute()`'s `ContractExeException` signature, and not caught anywhere along the block-processing call chain (`Manager.processTransaction` → `processBlock` → `applyBlock` → `pushBlock`, and `TronNetDelegate.processBlock`), which only catch specific checked exceptions such as `ContractValidateException`, `ContractExeException`, `TaposException`, etc. [2](#0-1) 

### Finding Description
`validate()` in the same actuator confirms the asset exists at validation time via `Commons.getAssetIssueStoreFinal(...).has(key)` [3](#0-2) , but `execute()` re-fetches the capsule independently and calls `.getNum()`/`.getTrxNum()` on it without re-checking for `null` [4](#0-3) . Because `validate()` and `execute()` of different transactions inside the same block are interleaved by `Manager.processTransaction`/`processBlock` [5](#0-4) , any state change to the `AssetIssueStore`/`AssetIssueV2Store` performed by an earlier transaction in the same block (or a maintenance-cycle side effect) between one transaction's `validate()` and its own `execute()` can make the asset lookup return `null` at execute time, triggering an unguarded NPE.

This exception is a `RuntimeException`, not one of the checked exception types the transaction/block pipeline declares or catches. `Manager.pushTransaction`, `applyBlock`, `processBlock`, and `TronNetDelegate.processBlock` all enumerate specific checked exceptions (`ContractValidateException`, `ContractExeException`, `ValidateSignatureException`, `TaposException`, etc.) and have no catch-all for `RuntimeException`/`Throwable` [6](#0-5) [7](#0-6) . Unlike `Wallet.broadcastTransaction`, which has a final `catch (Exception e)` guard that safely swallows unexpected runtime errors during standalone broadcast [8](#0-7) , no equivalent guard exists on the block-application path once a transaction is embedded in a block that must be applied by every node (both when the transaction is first pending/being packed and, more importantly, when every peer node replays that already-confirmed block during normal sync).

### Impact Explanation
If a transaction triggers this NPE while being applied as part of a block, the exception propagates out of `processTransaction`/`processBlock`/`applyBlock` uncaught by any of the declared checked-exception handlers along the chain. Because the transaction (once mined) is a permanent part of the chain, every node that replays that block during initial sync or after a restart will hit the identical unguarded NPE, mirroring the CVE's "malformed resource remains and the process re-enters a crash loop on every restart" pattern — except here the blast radius is the entire network of nodes that must eventually process that block, not a single ingress controller.

### Likelihood Explanation
This requires a specific ordering: a transaction whose `ParticipateAssetIssueContract` asset key is valid at `validate()` time but becomes unresolvable (`null`) in the `AssetIssueStore`/`AssetIssueV2Store` by the time `execute()` runs for that same transaction inside block processing. Reaching this precise state (e.g., via same-block interaction with another asset-issuance/store-mutating transaction, or a same-token-name toggle boundary) requires careful transaction crafting/ordering rather than a single trivially malformed field, so likelihood is moderate rather than trivial — but it only requires the privilege of an ordinary account that can broadcast `ParticipateAssetIssueContract` and (if applicable) a companion contract, not any special network role.

### Recommendation
Add a null check on the `AssetIssueCapsule` returned in `ParticipateAssetIssueActuator.execute()` (and audit sibling actuators — `TransferAssetActuator`, `ExchangeTransactionActuator`, `UpdateAssetActuator`, etc. — for the same re-fetch-without-null-check pattern) and throw a `ContractExeException` instead of allowing an NPE to propagate. More generally, block-application entry points (`Manager.processBlock`/`applyBlock`, `TronNetDelegate.processBlock`) should have a defensive `catch (RuntimeException e)` that converts unexpected runtime errors into a handled `BadBlockException`/rejection rather than letting them escape uncaught.

### Proof of Concept
1. Issue an asset `A` via `AssetIssueContract`.
2. Craft two transactions for the same block: transaction T1 that causes the `AssetIssueStore`/`AssetIssueV2Store` entry for `A` to be removed or replaced in a way `Commons.getAssetIssueStoreFinal(...).get(key)` returns `null`, and T2 = `ParticipateAssetIssueContract` referencing asset `A`, both broadcast so that T1 executes before T2 within the same block's `for (TransactionCapsule transactionCapsule : block.getTransactions())` loop [9](#0-8) .
3. When the block containing T1+T2 is applied, T2's `execute()` calls `assetIssueCapsule.getNum()` on a `null` reference, throwing an uncaught `NullPointerException` that is not caught by `ParticipateAssetIssueActuator.execute()`'s catch clause [1](#0-0)  nor by `processBlock`'s checked-exception handlers, crashing block application for every node that later applies this block.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ParticipateAssetIssueActuator.java (L73-97)
```java
      AssetIssueCapsule assetIssueCapsule;
      assetIssueCapsule = Commons
          .getAssetIssueStoreFinal(dynamicStore, assetIssueStore, assetIssueV2Store).get(key);

      long exchangeAmount = multiplyExact(cost, assetIssueCapsule.getNum());
      exchangeAmount = floorDiv(exchangeAmount, assetIssueCapsule.getTrxNum());
      ownerAccount.addAssetAmountV2(key, exchangeAmount, dynamicStore, assetIssueStore);

      //add to to_address
      byte[] toAddress = participateAssetIssueContract.getToAddress().toByteArray();
      AccountCapsule toAccount = accountStore.get(toAddress);
      toAccount.setBalance(addExact(toAccount.getBalance(), cost));
      if (!toAccount.reduceAssetAmountV2(key, exchangeAmount, dynamicStore, assetIssueStore)) {
        throw new ContractExeException("reduceAssetAmount failed !");
      }

      //write to db
      accountStore.put(ownerAddress, ownerAccount);
      accountStore.put(toAddress, toAccount);
      ret.setStatus(fee, Protocol.Transaction.Result.code.SUCESS);
    } catch (InvalidProtocolBufferException | ArithmeticException e) {
      logger.debug(e.getMessage(), e);
      ret.setStatus(fee, code.FAILED);
      throw new ContractExeException(e.getMessage());
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ParticipateAssetIssueActuator.java (L128-132)
```java
    //Parameters check
    byte[] ownerAddress = participateAssetIssueContract.getOwnerAddress().toByteArray();
    byte[] toAddress = participateAssetIssueContract.getToAddress().toByteArray();
    byte[] assetName = participateAssetIssueContract.getAssetName().toByteArray();
    long amount = participateAssetIssueContract.getAmount();
```

**File:** framework/src/main/java/org/tron/core/net/TronNetDelegate.java (L251-322)
```java
  public void processBlock(BlockCapsule block, boolean isSync) throws P2pException {
    if (!hitDown && dbManager.getLatestSolidityNumShutDown() > 0
        && dbManager.getLatestSolidityNumShutDown() == dbManager.getDynamicPropertiesStore()
        .getLatestBlockHeaderNumberFromDB()) {

      logger.info("Begin shutdown, currentBlockNum:{}, DbBlockNum:{}, solidifiedBlockNum:{}",
          dbManager.getDynamicPropertiesStore().getLatestBlockHeaderNumber(),
          dbManager.getDynamicPropertiesStore().getLatestBlockHeaderNumberFromDB(),
          dbManager.getDynamicPropertiesStore().getLatestSolidifiedBlockNum());
      hitDown = true;
      LockSupport.unpark(hitThread);
      return;
    }
    if (hitDown) {
      return;
    }
    BlockId blockId = block.getBlockId();
    synchronized (blockLock) {
      try {
        if (freshBlockId.getIfPresent(blockId) == null) {
          if (block.getNum() <= getHeadBlockId().getNum()) {
            logger.warn("Receive a fork block {} witness {}, head {}",
                block.getBlockId().getString(),
                Hex.toHexString(block.getWitnessAddress().toByteArray()),
                getHeadBlockId().getString());
          }
          if (!isSync) {
            //record metrics
            metricsService.applyBlock(block);
          }
          dbManager.getBlockedTimer().set(Metrics.histogramStartTimer(
              MetricKeys.Histogram.LOCK_ACQUIRE_LATENCY, MetricLabels.BLOCK));
          Histogram.Timer timer = Metrics.histogramStartTimer(
              MetricKeys.Histogram.BLOCK_PROCESS_LATENCY, String.valueOf(isSync));
          dbManager.pushBlock(block);
          Metrics.histogramObserve(timer);
          freshBlockId.put(blockId, System.currentTimeMillis());
          logger.info("Success process block {}", blockId.getString());
          if (!backupServerStartFlag
              && System.currentTimeMillis() - block.getTimeStamp() < BLOCK_PRODUCED_INTERVAL) {
            backupServerStartFlag = true;
            backupServer.initServer();
          }
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
    }
```

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L1053-1067)
```java
  private void applyBlock(BlockCapsule block) throws ContractValidateException,
      ContractExeException, ValidateSignatureException, AccountResourceInsufficientException,
      TransactionExpirationException, TooBigTransactionException, DupTransactionException,
      TaposException, ValidateScheduleException, ReceiptCheckErrException,
      VMIllegalException, TooBigTransactionResultException,
      ZksnarkException, BadBlockException, EventBloomException {
    applyBlock(block, block.getTransactions());
  }

  private void applyBlock(BlockCapsule block, List<TransactionCapsule> txs)
      throws ContractValidateException, ContractExeException, ValidateSignatureException,
      AccountResourceInsufficientException, TransactionExpirationException,
      TooBigTransactionException, DupTransactionException, TaposException,
      ValidateScheduleException, ReceiptCheckErrException, VMIllegalException,
      TooBigTransactionResultException, ZksnarkException, BadBlockException, EventBloomException {
```

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L1884-1902)
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
        accountStateCallBack.exeTransFinish();
        if (Objects.nonNull(result)) {
          results.add(result);
        }
      }
```

**File:** framework/src/main/java/org/tron/core/Wallet.java (L624-629)
```java
    } catch (Exception e) {
      logger.warn("Broadcast transaction {} failed", txID, e);
      return builder.setResult(false).setCode(response_code.OTHER_ERROR)
          .setMessage(ByteString.copyFromUtf8("Error: " + e.getMessage()))
          .build();
    }
```
