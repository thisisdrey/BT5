## Analog Found

### Title
Unchecked return value of `AccountCapsule.addAsset` / `addAssetV2` in `AssetIssueActuator.execute` can silently drop newly issued token supply - (File: actuator/src/main/java/org/tron/core/actuator/AssetIssueActuator.java)

### Summary
`AccountCapsule.addAsset()` and `AccountCapsule.addAssetV2()` are boolean "insert-once" methods that intentionally return `false` instead of overwriting an existing map entry when the target key is already present [1](#0-0) . `AssetIssueActuator.execute()` calls both methods to credit the newly-issued token supply to the issuer's account but never checks the returned boolean [2](#0-1) . This mirrors the reported pattern of `dispatcher.add()` in the Solidity `Factory.createMultipool` — a duplicate-prevention "add" whose false-on-duplicate result is discarded, allowing the surrounding logic to proceed as if the insert had succeeded.

### Finding Description
`AssetIssueActuator.execute()` unpacks the `AssetIssueContract`, allocates a new `tokenIdNum`, persists the `AssetIssueCapsule`/`AssetIssueCapsuleV2` to the asset stores, deducts the issuance fee from the owner, and then attempts to credit the owner account with the remaining (non-frozen) supply: [3](#0-2) 
Neither `accountCapsule.addAsset(...)` nor `accountCapsule.addAssetV2(...)` return values are inspected. Both methods are explicitly designed to fail closed (`return false`) rather than clobber an existing balance entry for the same key [1](#0-0) . Regardless of whether the insert succeeds or silently fails, execution falls through to `ret.setStatus(fee, code.SUCESS)` and the actuator commits the account, the fee deduction, the new `AssetIssueCapsule`, and the incremented `tokenIdNum` — all as if the token supply had been correctly credited [4](#0-3) . This is the same class of defect as the reported issue: a boolean "insert, fail on duplicate" primitive whose result is discarded by the caller, letting downstream state (fee already charged, token record already created) diverge from the actual credited balance.

### Impact Explanation
If the credit call returns `false` for either the V1 (`assetMap`, keyed by asset name) or V2 (`assetV2Map`, keyed by token id) map, the transaction still finalizes with `SUCESS`: the issuer's TRX fee is spent, the asset is recorded in `AssetIssueStore`/`AssetIssueV2Store` with a fixed `totalSupply`, but the corresponding tokens are never credited to any account — resulting in unbacked/missing token supply (the issued asset's `totalSupply` no longer matches sum of holder balances) and permanent loss of the freshly minted tokens for the issuer. This falls under "unbacked balance" / "permanent freezing of funds" categories.

### Likelihood Explanation
Reaching this code path only requires broadcasting a normal `AssetIssueContract` transaction (anonymous asset issuer, no special privilege). The current `validate()` logic makes it hard to independently confirm a collision is reachable under every configuration (e.g., interactions between `allowSameTokenName` toggling and account state were not fully traceable from the available snippets), so likelihood is uncertain without deeper trace-through of every `AssetIssueCapsule.createDbKey()`/`createDbV2Key()` derivation and all mutation paths of `assetMap`/`assetV2Map`. This is a genuine code smell (ignored fail-closed return values in a balance-crediting path) but I could not fully verify a concrete transaction sequence that forces `addAsset`/`addAssetV2` to return `false` in the current codebase using the tools available.

### Recommendation
Check the boolean return values of `accountCapsule.addAsset(...)` and `accountCapsule.addAssetV2(...)` in `AssetIssueActuator.execute()`; if either returns `false`, abort the transaction with a `ContractExeException` (mirroring how `TransferAssetActuator` already checks `reduceAssetAmountV2`'s return value) [5](#0-4)  instead of allowing the actuator to report success while silently dropping the newly issued supply.

### Proof of Concept
Not fully constructible from static review alone — the visible `validate()` logic includes a global "Token exists" check against `AssetIssueStore` only for the `allowSameTokenName == 0` case, and a full walk of all code paths that populate an account's `assetMap`/`assetV2Map` (e.g. via prior issuance, `ParticipateAssetIssueActuator`, or governance parameter changes to `allowSameTokenName`) would be required to demonstrate a concrete collision. This should be verified with a live/test-node reproduction (e.g., toggling `allowSameTokenName` between issuances for the same account/name) rather than asserted definitively from the indexed snippets.

### Citations

**File:** chainbase/src/main/java/org/tron/core/capsule/AccountCapsule.java (L828-847)
```java
  public boolean addAsset(byte[] key, long value) {
    Map<String, Long> assetMap = this.account.getAssetMap();
    String nameKey = ByteArray.toStr(key);
    if (!assetMap.isEmpty() && assetMap.containsKey(nameKey)) {
      return false;
    }
    this.account = this.account.toBuilder().putAsset(nameKey, value).build();
    return true;
  }

  public boolean addAssetV2(byte[] key, long value) {
    if (AssetUtil.hasAssetV2(this.account, key)) {
      return false;
    }

    this.account = this.account.toBuilder()
        .putAssetV2(ByteArray.toStr(key), value)
        .build();
    return true;
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/AssetIssueActuator.java (L113-132)
```java
      if (dynamicStore.getAllowSameTokenName() == 0) {
        accountCapsule.addAsset(assetIssueCapsule.createDbKey(), remainSupply);
      }
      accountCapsule.setAssetIssuedName(assetIssueCapsule.createDbKey());
      accountCapsule.setAssetIssuedID(assetIssueCapsule.createDbV2Key());
      accountCapsule.addAssetV2(assetIssueCapsuleV2.createDbV2Key(), remainSupply);
      accountCapsule.setInstance(accountCapsule.getInstance().toBuilder()
          .addAllFrozenSupply(frozenList).build());

      accountStore.put(ownerAddress, accountCapsule);

      ret.setAssetIssueID(Long.toString(tokenIdNum));
      ret.setStatus(fee, code.SUCESS);
    } catch (InvalidProtocolBufferException | BalanceInsufficientException | ArithmeticException e) {
      logger.debug(e.getMessage(), e);
      ret.setStatus(fee, code.FAILED);
      throw new ContractExeException(e.getMessage());
    }

    return true;
```

**File:** actuator/src/main/java/org/tron/core/actuator/TransferAssetActuator.java (L76-79)
```java
      if (!ownerAccountCapsule
          .reduceAssetAmountV2(assetName.toByteArray(), amount, dynamicStore, assetIssueStore)) {
        throw new ContractExeException("reduceAssetAmount failed !");
      }
```
