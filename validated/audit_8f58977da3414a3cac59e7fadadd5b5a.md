## Title
Free CPU-exhaustion DoS via `ShieldedTransferContract` transactions that bypass bandwidth fees and never pay the shielded fee on invalid zk-SNARK proofs - (File: `actuator/src/main/java/org/tron/core/actuator/ShieldedTransferActuator.java`, `chainbase/src/main/java/org/tron/core/db/BandwidthProcessor.java`)

## Summary
`ShieldedTransferContract` transactions are explicitly excluded from bandwidth/net-fee accounting in `BandwidthProcessor.consume()`, and the real monetary "shielded transaction fee" is only burned inside `ShieldedTransferActuator.execute()`, which never runs if `validate()` fails. Because the expensive Sapling zk-SNARK proof verification (native `librustzcash` pairing checks) happens inside `validate()` *before* any fee is charged, an unauthenticated broadcaster can submit an unlimited stream of fully-shielded (no transparent in/out) transactions containing garbage spend/receive descriptions, forcing every full node/witness to perform costly elliptic-curve verification for zero cost — an exact analog of the Aleo report's "free `split` proof DOS".

## Finding Description
In `BandwidthProcessor.consume`, `ShieldedTransferContract` is special-cased to skip the entire bandwidth/net-fee accounting loop: [1](#0-0) 

This means submitting such a transaction costs nothing in bandwidth/net fee, regardless of account resources.

The only monetary cost associated with a shielded transfer is the `shieldedTransactionFee`, which is deducted from the shielded pool inside `execute()`: [2](#0-1) 

But `execute()` is only reached if `validate()` succeeds. `validate()` first performs cheap structural checks (`checkSender`, `checkReceiver`, `validateTransparent`), none of which require the caller to own real value for a shield-to-shield transfer with no transparent addresses: [3](#0-2) 

Then it invokes `checkProof`, which performs the actual expensive cryptographic work — native Sapling verification context init, `librustzcashSaplingCheckSpend`, `librustzcashSaplingCheckOutput`, and `librustzcashSaplingFinalCheck` (elliptic-curve pairing operations) — on attacker-controlled byte arrays before failing: [4](#0-3) 

Since `checkProof` is only cached per exact `transactionId` (`ZKProofStore`), an attacker trivially varies the spend/receive fields (nullifier, anchor, rk, zkproof, cv, epk, etc.) to produce a fresh transaction id for every submission, defeating the cache and forcing a brand-new expensive native verification each time: [5](#0-4) 

This validate path is reached directly from the public `Wallet.broadcastTransaction` → `Manager.pushTransaction` → `Manager.processTransaction` flow, which calls `consumeBandwidth` (a no-op for shielded contracts) and then `trace.exec()` (which runs the actuator's `validate()`/`execute()`), all before any block is produced: [6](#0-5) [7](#0-6) 

The only mitigating control is `shieldedTransInPendingMaxCounts`, which merely caps concurrently *pending* shielded transactions; it does not throttle the CPU cost incurred processing (and rejecting) an unbounded stream of failing shielded transactions submitted sequentially/from many accounts.

## Impact Explanation
Any unauthenticated party with a signed transaction (needing only a valid signature and an existing account — not any real shielded value) can force every validating full node and witness to repeatedly execute costly elliptic-curve pairing cryptography for free. Because bandwidth accounting is bypassed entirely for this contract type, and the real "shielded transaction fee" is never charged when the proof check fails, this creates a nearly cost-free CPU-exhaustion vector against nodes validating the p2p transaction stream and witnesses attempting to pack blocks, degrading transaction throughput/liveness — matching the "node can no longer serve" / degraded block production impact class.

## Likelihood Explanation
High. `ShieldedTransferContract` transactions require only a valid signer account (no real shielded notes/value is validated until the proof check runs), the bandwidth-fee skip is unconditional for this type, and building malformed spend/receive descriptions with unique byte content (to avoid the proof cache) is trivial. Shielded transactions require `dynamicStore.supportShieldedTransaction()`, but on any network with shielded transfers enabled this is directly and repeatedly exploitable by an anonymous API client.

## Recommendation
Do not exempt `ShieldedTransferContract` from bandwidth/net fee accounting in `BandwidthProcessor.consume` — require the same collateral bandwidth/fee cost as other contract types regardless of whether zk-proof verification later succeeds. Additionally, consider charging (and only refunding on success) a minimum fee/energy cost before invoking the native Sapling proof verification in `ShieldedTransferActuator.checkProof`, so failed proof verification cannot be repeated for free.

## Proof of Concept
1. Create/own any account with a valid key (no shielded balance needed).
2. Build a `ShieldedTransferContract` with no `transparent_from_address`/`transparent_to_address`, one `SpendDescription`, and one `ReceiveDescription` populated with random/garbage 32/192/64-byte fields (satisfies `checkSender`/`checkReceiver`/`validateTransparent` structural checks).
3. Sign and call `Wallet.broadcastTransaction`.
4. Observe in `Manager.processTransaction` that `consumeBandwidth` charges nothing (contract type skipped in `BandwidthProcessor.consume`), then `trace.exec()` invokes `ShieldedTransferActuator.validate()` → `checkProof()`, running full native Sapling verification before throwing `ZkProofValidateException`.
5. Repeat with mutated byte fields (new transaction id each time) in a loop from one or many accounts to keep forcing expensive verification at zero fee cost.

### Citations

**File:** chainbase/src/main/java/org/tron/core/db/BandwidthProcessor.java (L122-125)
```java
    for (Contract contract : contracts) {
      if (contract.getType() == ShieldedTransferContract) {
        continue;
      }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ShieldedTransferActuator.java (L73-87)
```java
    long fee = calcFee(shieldedTransferContract);
    try {
      if (shieldedTransferContract.getTransparentFromAddress().toByteArray().length > 0) {
        executeTransparentFrom(shieldedTransferContract.getTransparentFromAddress().toByteArray(),
            shieldedTransferContract.getFromAmount(), ret, fee);
      }
      Commons.adjustAssetBalanceV2(accountStore.getBlackhole(),
          CommonParameter.getInstance().getZenTokenId(), fee,
          accountStore, assetIssueStore, dynamicStore);
    } catch (BalanceInsufficientException e) {
      logger.debug(e.getMessage(), e);
      ret.setStatus(0, code.FAILED);
      ret.setShieldedTransactionFee(fee);
      throw new ContractExeException(e.getMessage());
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ShieldedTransferActuator.java (L223-245)
```java
    long fee = calcFee(shieldedTransferContract);
    //transparent verification
    checkSender(shieldedTransferContract);
    checkReceiver(shieldedTransferContract);
    validateTransparent(shieldedTransferContract, fee);

    List<SpendDescription> spendDescriptions = shieldedTransferContract.getSpendDescriptionList();
    // check duplicate sapling nullifiers
    if (CollectionUtils.isNotEmpty(spendDescriptions)) {
      HashSet<ByteString> nfSet = new HashSet<>();
      for (SpendDescription spendDescription : spendDescriptions) {
        if (nfSet.contains(spendDescription.getNullifier())) {
          throw new ContractValidateException("duplicate sapling nullifiers in this transaction");
        }
        nfSet.add(spendDescription.getNullifier());
        if (!merkleContainer.merkleRootExist(spendDescription.getAnchor().toByteArray())) {
          throw new ContractValidateException("Rt is invalid.");
        }
        if (nullifierStore.has(spendDescription.getNullifier().toByteArray())) {
          throw new ContractValidateException("note has been spend in this transaction");
        }
      }
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ShieldedTransferActuator.java (L275-286)
```java
  private void checkProof(List<SpendDescription> spendDescriptions,
      List<ReceiveDescription> receiveDescriptions, long fee) throws ZkProofValidateException {
    DynamicPropertiesStore dynamicStore = chainBaseManager.getDynamicPropertiesStore();
    ZKProofStore proofStore = chainBaseManager.getProofStore();
    if (proofStore.has(tx.getTransactionId().getBytes())) {
      if (proofStore.get(tx.getTransactionId().getBytes())) {
        return;
      } else {
        throw new ZkProofValidateException("record is fail, skip proof", false);
      }
    }

```

**File:** actuator/src/main/java/org/tron/core/actuator/ShieldedTransferActuator.java (L287-354)
```java
    byte[] signHash = getShieldTransactionHashIgnoreTypeException(tx.getInstance());

    if (CollectionUtils.isNotEmpty(spendDescriptions)
        || CollectionUtils.isNotEmpty(receiveDescriptions)) {
      long ctx = JLibrustzcash.librustzcashSaplingVerificationCtxInit();
      try {
        for (SpendDescription spendDescription : spendDescriptions) {
          if (!JLibrustzcash.librustzcashSaplingCheckSpend(
              new CheckSpendParams(ctx,
                  spendDescription.getValueCommitment().toByteArray(),
                  spendDescription.getAnchor().toByteArray(),
                  spendDescription.getNullifier().toByteArray(),
                  spendDescription.getRk().toByteArray(),
                  spendDescription.getZkproof().toByteArray(),
                  spendDescription.getSpendAuthoritySignature().toByteArray(),
                  signHash)
          )) {
            throw new ZkProofValidateException("librustzcashSaplingCheckSpend error", true);
          }
        }

        for (ReceiveDescription receiveDescription : receiveDescriptions) {
          if (receiveDescription.getCEnc().size() != ZC_ENCCIPHERTEXT_SIZE
              || receiveDescription.getCOut().size() != ZC_OUTCIPHERTEXT_SIZE) {
            throw new ZkProofValidateException("Cout or CEnc size error", true);
          }
          if (!JLibrustzcash.librustzcashSaplingCheckOutput(
              new CheckOutputParams(ctx,
                  receiveDescription.getValueCommitment().toByteArray(),
                  receiveDescription.getNoteCommitment().toByteArray(),
                  receiveDescription.getEpk().toByteArray(),
                  receiveDescription.getZkproof().toByteArray())
          )) {
            throw new ZkProofValidateException("librustzcashSaplingCheckOutput error", true);
          }
        }

        long valueBalance;
        long totalShieldedPoolValue = dynamicStore
            .getTotalShieldedPoolValue();
        try {
          valueBalance = addExact(subtractExact(
              shieldedTransferContract.getToAmount(),
              shieldedTransferContract.getFromAmount()), fee);
          totalShieldedPoolValue = subtractExact(
              totalShieldedPoolValue, valueBalance);
        } catch (ArithmeticException e) {
          logger.debug(e.getMessage(), e);
          throw new ZkProofValidateException(e.getMessage(), true);
        }

        if (totalShieldedPoolValue < 0) {
          throw new ZkProofValidateException("shieldedPoolValue error", true);
        }

        if (!JLibrustzcash.librustzcashSaplingFinalCheck(
            new FinalCheckParams(ctx,
                valueBalance,
                shieldedTransferContract.getBindingSignature().toByteArray(),
                signHash)
        )) {
          throw new ZkProofValidateException("librustzcashSaplingFinalCheck error", true);
        }
      } catch (ZksnarkException e) {
        throw new ZkProofValidateException(e.getMessage(), true);
      } finally {
        JLibrustzcash.librustzcashSaplingVerificationCtxFree(ctx);
      }
```

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L886-936)
```java
  public boolean pushTransaction(final TransactionCapsule trx)
      throws ValidateSignatureException, ContractValidateException, ContractExeException,
      AccountResourceInsufficientException, DupTransactionException, TaposException,
      TooBigTransactionException, TransactionExpirationException,
      ReceiptCheckErrException, VMIllegalException, TooBigTransactionResultException {

    if (isShieldedTransaction(trx.getInstance()) && !chainBaseManager.getDynamicPropertiesStore()
        .supportShieldedTransaction()) {
      throw new ContractValidateException("ShieldedTransferContract is not supported.");
    }

    if (isExchangeTransaction(trx.getInstance())) {
      throw new ContractValidateException("ExchangeTransactionContract is rejected");
    }

    pushTransactionQueue.add(trx);
    Metrics.gaugeInc(MetricKeys.Gauge.MANAGER_QUEUE, 1,
        MetricLabels.Gauge.QUEUE_QUEUED);
    try {
      if (!trx.validateSignature(chainBaseManager.getAccountStore(),
          chainBaseManager.getDynamicPropertiesStore())) {
        throw new ValidateSignatureException(String.format("trans sig validate failed, id: %s",
            trx.getTransactionId()));
      }

      synchronized (transactionLock) {
        while (true) {
          try {
            if (isBlockWaitingLock()) {
              TimeUnit.MILLISECONDS.sleep(SLEEP_FOR_WAIT_LOCK);
            } else {
              break;
            }
          } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            logger.debug("The wait has been interrupted.");
          }
        }
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
```

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L1551-1561)
```java
    TransactionTrace trace = new TransactionTrace(trxCap, StoreFactory.getInstance(),
        new RuntimeImpl());
    trxCap.setTrxTrace(trace);

    consumeBandwidth(trxCap, trace);
    consumeMultiSignFee(trxCap, trace);
    consumeMemoFee(trxCap, trace);

    trace.init(blockCap, eventPluginLoaded);
    trace.checkIsConstant();
    trace.exec();
```
