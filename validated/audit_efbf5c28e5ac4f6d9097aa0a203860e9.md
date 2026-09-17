### Title
Asset issuer can instantly change `free_asset_net_limit` / `public_free_asset_net_limit` with no timelock, causing unexpected TRX fee loss for token holders mid-transaction - ([File: actuator/src/main/java/org/tron/core/actuator/UpdateAssetActuator.java])

### Summary
`UpdateAssetActuator` lets an asset issuer change `free_asset_net_limit` (per-account free bandwidth for the token) and `public_free_asset_net_limit` (global free bandwidth for the token) at any time, taking effect immediately in the very next block, with no delay and no bound relative to the previous value (only an absolute range check against `oneDayNetLimit`). This mirrors the reported `originationFee` issue: a value a normal user's transaction economics depend on can be changed suddenly by the controlling party (here, the token issuer instead of a lending-protocol admin), causing unexpected fund loss to an unrelated, unprivileged account (a token holder broadcasting a `TransferAssetContract`).

### Finding Description
`UpdateAssetContract` is validated in [1](#0-0)  where the only checks on the new limits are that they are non-negative and less than `dynamicStore.getOneDayNetLimit()`. There is no minimum change interval, no maximum delta from the current value, and no grace period before the new value takes effect — `execute()` writes the new limits straight into the `AssetIssueCapsule` at [2](#0-1) .

These limits are read live, at execution time of every `TransferAssetContract`, by `BandwidthProcessor.useAssetAccountNet`: [3](#0-2) . If the currently configured `publicFreeAssetNetLimit`/`freeAssetNetLimit` is insufficient for the pending transfer, `useAssetAccountNet` returns `false`, and the bandwidth-consumption chain falls through to `useAccountNet`, then `useFreeNet`, then finally `useTransactionFee`, which burns actual TRX from the sender's balance: [4](#0-3) .

Because a user signs and broadcasts a `TransferAssetContract` expecting to consume the token's free bandwidth allowance (as advertised by the asset's current `free_asset_net_limit`/`public_free_asset_net_limit`), the issuer can submit an `UpdateAssetContract` transaction lowering these limits (e.g., to 0) which lands in the same or an earlier block. When the user's transfer is then processed, the free-bandwidth path silently fails and the account is charged TRX fees it did not budget for — an unexpected balance deduction the sender never explicitly agreed to at signing time, entirely analogous to a borrower losing funds because `originationFee` changed between the moment they decided to borrow and the moment their transaction executed.

### Impact Explanation
The impacted party is an unprivileged, unrelated user (any holder of the asset performing a `TransferAssetContract`), not the issuer. The loss is a real TRX balance deduction (`consumeFeeForBandwidth`) that the user did not anticipate, caused entirely by the issuer's unbounded, instantaneous parameter change. This is a fund-loss bug class matching the reported issue's core concern (no timelock/bound on a fee-affecting parameter), though the maximum loss per transaction is bounded by `transactionFee * bytes`, making it a lower-magnitude analog than the original DeFi lending scenario.

### Likelihood Explanation
Likelihood is moderate: it requires the asset issuer (a role with legitimate control over the asset's parameters — not a validator/committee/witness) to submit an `UpdateAssetContract` transaction, which is a completely ordinary, permitted action with no cooldown restriction. No unusual timing/race precision is even required — the issuer simply lowers the limit and any subsequent transfer by holders is affected until the issuer chooses to raise it again.

### Recommendation
- Add a minimum time delay (timelock) before a decreased `free_asset_net_limit`/`public_free_asset_net_limit` takes effect, giving token holders time to react.
- Bound the magnitude of change per update (e.g., percentage-based rate limiting) similar to how `ProposalUtil` bounds other parameter changes.
- Alternatively, make the bandwidth check use the limit value in effect when the account's usage window began, rather than the value at the moment of the specific transaction's execution.

### Proof of Concept
1. Asset issuer issues token `T` with `free_asset_net_limit` = 5000 and `public_free_asset_net_limit` = 5000, both non-zero so holders normally transfer for free.
2. A holder signs and broadcasts a `TransferAssetContract` for `T`, expecting free bandwidth to cover it (per `useAssetAccountNet` logic in [5](#0-4) ).
3. Before that transaction is packed into a block, the issuer broadcasts an `UpdateAssetContract` with `new_limit = 0` and `new_public_limit = 0` (allowed unconditionally per [6](#0-5) ), which the block producer includes first.
4. The holder's `TransferAssetContract` is then processed with the new zero limits; `useAssetAccountNet` fails, `useAccountNet`/`useFreeNet` also fail (assuming account bandwidth exhausted), and `useTransactionFee` deducts real TRX from the holder's balance — a fund loss the holder never consented to when they signed the transaction.

Note: I could not verify from the index whether any downstream mempool/API layer re-validates fee-sensitivity at broadcast time versus execution time, or whether there is a minimum interval enforced elsewhere (e.g., via a separate cooldown check not indexed). This should be verified against the full `UpdateAssetActuator`/`ProposalUtil` code paths in a live checkout if precise confirmation is needed.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/UpdateAssetActuator.java (L59-78)
```java
      assetIssueCapsuleV2.setFreeAssetNetLimit(newLimit);
      assetIssueCapsuleV2.setPublicFreeAssetNetLimit(newPublicLimit);
      assetIssueCapsuleV2.setUrl(newUrl);
      assetIssueCapsuleV2.setDescription(newDescription);

      if (dynamicStore.getAllowSameTokenName() == 0) {
        assetIssueCapsule = assetIssueStore.get(accountCapsule.getAssetIssuedName().toByteArray());
        assetIssueCapsule.setFreeAssetNetLimit(newLimit);
        assetIssueCapsule.setPublicFreeAssetNetLimit(newPublicLimit);
        assetIssueCapsule.setUrl(newUrl);
        assetIssueCapsule.setDescription(newDescription);

        assetIssueStore
            .put(assetIssueCapsule.createDbKey(), assetIssueCapsule);
        assetIssueStoreV2
            .put(assetIssueCapsuleV2.createDbV2Key(), assetIssueCapsuleV2);
      } else {
        assetIssueV2Store
            .put(assetIssueCapsuleV2.createDbV2Key(), assetIssueCapsuleV2);
      }
```

**File:** actuator/src/main/java/org/tron/core/actuator/UpdateAssetActuator.java (L116-166)
```java
    long newLimit = updateAssetContract.getNewLimit();
    long newPublicLimit = updateAssetContract.getNewPublicLimit();
    byte[] ownerAddress = updateAssetContract.getOwnerAddress().toByteArray();
    ByteString newUrl = updateAssetContract.getUrl();
    ByteString newDescription = updateAssetContract.getDescription();

    if (!DecodeUtil.addressValid(ownerAddress)) {
      throw new ContractValidateException("Invalid ownerAddress");
    }

    AccountCapsule account = accountStore.get(ownerAddress);
    if (account == null) {
      throw new ContractValidateException("Account does not exist");
    }

    if (dynamicStore.getAllowSameTokenName() == 0) {
      if (account.getAssetIssuedName().isEmpty()) {
        throw new ContractValidateException("Account has not issued any asset");
      }

      if (assetIssueStore.get(account.getAssetIssuedName().toByteArray())
          == null) {
        throw new ContractValidateException("Asset is not existed in AssetIssueStore");
      }
    } else {
      if (account.getAssetIssuedID().isEmpty()) {
        throw new ContractValidateException("Account has not issued any asset");
      }

      if (assetIssueV2Store.get(account.getAssetIssuedID().toByteArray())
          == null) {
        throw new ContractValidateException("Asset is not existed in AssetIssueV2Store");
      }
    }

    if (!TransactionUtil.validUrl(newUrl.toByteArray())) {
      throw new ContractValidateException("Invalid url");
    }

    if (!TransactionUtil.validAssetDescription(newDescription.toByteArray())) {
      throw new ContractValidateException("Invalid description");
    }

    if (newLimit < 0 || newLimit >= dynamicStore.getOneDayNetLimit()) {
      throw new ContractValidateException("Invalid FreeAssetNetLimit");
    }

    if (newPublicLimit < 0 || newPublicLimit >=
        dynamicStore.getOneDayNetLimit()) {
      throw new ContractValidateException("Invalid PublicFreeAssetNetLimit");
    }
```

**File:** chainbase/src/main/java/org/tron/core/db/BandwidthProcessor.java (L160-190)
```java
      if (useAccountNet(accountCapsule, bytesSize, now)) {
        continue;
      }

      if (useFreeNet(accountCapsule, bytesSize, now)) {
        continue;
      }

      if (useTransactionFee(accountCapsule, bytesSize, trace)) {
        continue;
      }

      long fee = chainBaseManager.getDynamicPropertiesStore().getTransactionFee() * bytesSize;
      throw new AccountResourceInsufficientException(
          String.format(
              "account [%s] has insufficient bandwidth[%d] and balance[%d] to create new account",
              StringUtil.encode58Check(address), bytesSize, fee));
    }
  }

  private boolean useTransactionFee(AccountCapsule accountCapsule, long bytes,
      TransactionTrace trace) {
    long fee = chainBaseManager.getDynamicPropertiesStore().getTransactionFee() * bytes;
    if (consumeFeeForBandwidth(accountCapsule, fee)) {
      trace.setNetBill(0, fee);
      chainBaseManager.getDynamicPropertiesStore().addTotalTransactionCost(fee);
      return true;
    } else {
      return false;
    }
  }
```

**File:** chainbase/src/main/java/org/tron/core/db/BandwidthProcessor.java (L290-353)
```java
  private boolean useAssetAccountNet(Contract contract, AccountCapsule accountCapsule, long now,
      long bytes)
      throws ContractValidateException {

    ByteString assetName;
    try {
      assetName = contract.getParameter().unpack(TransferAssetContract.class).getAssetName();
    } catch (Exception ex) {
      throw new RuntimeException(ex.getMessage());
    }

    AssetIssueCapsule assetIssueCapsule;
    AssetIssueCapsule assetIssueCapsuleV2;
    assetIssueCapsule = Commons.getAssetIssueStoreFinal(
        chainBaseManager.getDynamicPropertiesStore(),
        chainBaseManager.getAssetIssueStore(), chainBaseManager.getAssetIssueV2Store())
        .get(assetName.toByteArray());
    if (assetIssueCapsule == null) {
      throw new ContractValidateException(String.format("asset [%s] does not exist", assetName));
    }

    String tokenName = ByteArray.toStr(assetName.toByteArray());
    String tokenID = assetIssueCapsule.getId();
    if (assetIssueCapsule.getOwnerAddress() == accountCapsule.getAddress()) {
      return useAccountNet(accountCapsule, bytes, now);
    }

    long publicFreeAssetNetLimit = assetIssueCapsule.getPublicFreeAssetNetLimit();
    long publicFreeAssetNetUsage = assetIssueCapsule.getPublicFreeAssetNetUsage();
    long publicLatestFreeNetTime = assetIssueCapsule.getPublicLatestFreeNetTime();

    long newPublicFreeAssetNetUsage = increase(publicFreeAssetNetUsage, 0,
        publicLatestFreeNetTime, now);

    if (bytes > (publicFreeAssetNetLimit - newPublicFreeAssetNetUsage)) {
      logger.debug("The {} public free bandwidth is not enough."
              + " Bytes: {}, publicFreeAssetNetLimit: {}, newPublicFreeAssetNetUsage: {}.",
          tokenID, bytes, publicFreeAssetNetLimit,  newPublicFreeAssetNetUsage);
      return false;
    }

    long freeAssetNetLimit = assetIssueCapsule.getFreeAssetNetLimit();

    long freeAssetNetUsage;
    long latestAssetOperationTime;
    if (chainBaseManager.getDynamicPropertiesStore().getAllowSameTokenName() == 0) {
      freeAssetNetUsage = accountCapsule
          .getFreeAssetNetUsage(tokenName);
      latestAssetOperationTime = accountCapsule
          .getLatestAssetOperationTime(tokenName);
    } else {
      freeAssetNetUsage = accountCapsule.getFreeAssetNetUsageV2(tokenID);
      latestAssetOperationTime = accountCapsule.getLatestAssetOperationTimeV2(tokenID);
    }

    long newFreeAssetNetUsage = increase(freeAssetNetUsage, 0,
        latestAssetOperationTime, now);

    if (bytes > (freeAssetNetLimit - newFreeAssetNetUsage)) {
      logger.debug("The {} free bandwidth is not enough."
              + " Bytes: {}, freeAssetNetLimit: {}, newFreeAssetNetUsage:{}.",
          tokenID, bytes, freeAssetNetLimit, newFreeAssetNetUsage);
      return false;
    }
```
