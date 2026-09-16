### Title
Desynchronized `asset`/`assetV2` balance mappings after SELFDESTRUCT token sweep can be exploited to mint unbacked TRC10 balance - (File: `actuator/src/main/java/org/tron/core/vm/utils/MUtil.java`)

### Summary
`YetiToken`'s bug (parallel `balances`/`_balances` and `allowed`/`_allowances` maps kept out of sync because only some functions update both) has a structural analog in java-tron's TRC10 asset accounting: accounts keep two parallel balance maps, the legacy `asset` map and the `assetV2` map. `MUtil.transferAllToken()`, invoked on contract `SELFDESTRUCT`, only clears/moves the `assetV2` map and never touches the legacy `asset` map. `AccountCapsule.reduceAssetAmountV2()` — used by every ordinary TRC10 transfer while `AllowSameTokenName == 0` — trusts the legacy `asset` map as the "source of truth" and blindly rewrites `assetV2` to match it, discarding whatever the real `assetV2` balance was.

### Finding Description
Every `AccountCapsule` stores the same TRC10 token balance in two maps for legacy compatibility: `asset` (v1) and `assetV2` (v2), exactly the double-bookkeeping pattern flagged in the external report.

When a smart contract self-destructs and forwards its whole token balance to a beneficiary, `Program.java` calls `MUtil.transferAllToken`, which iterates only `fromAccountCap.getAssetMapV2()` and zeroes/moves values in the `assetV2` builder — the legacy `asset` map on the "from" account is left completely untouched: [1](#0-0) 

Separately, ordinary TRC10 transfers (`TransferAssetActuator`, `ParticipateAssetIssueActuator`, VM `TransferToken`, etc.) call `AccountCapsule.reduceAssetAmountV2()`/`addAssetAmountV2()`. When `dynamicPropertiesStore.getAllowSameTokenName() == 0`, this method reads the *current amount from the legacy `asset` map* (not from `assetV2`) to authorize the operation, and then writes that same v1-derived amount into **both** `asset` and `assetV2`: [2](#0-1) 

Because `transferAllToken` never decremented the legacy `asset` map, an account whose real (v2) balance has already been swept away by a prior `SELFDESTRUCT` still shows its original amount in `asset`. A subsequent legacy-path transfer (`AllowSameTokenName == 0`) will pass the balance check against the stale `asset` value and then set `assetV2` back up to that stale amount — recreating TRC10 token balance that is no longer backed by any real supply movement, i.e., minting unbacked balance out of the desynchronized duplicate maps.

### Impact Explanation
This allows an attacker who controls a contract (deployable by anyone) to: (1) issue/acquire a TRC10 token balance on a contract account, (2) `SELFDESTRUCT` the contract to sweep the `assetV2` balance to a beneficiary while leaving the legacy `asset` map stale/non-zero, then (3) trigger a legacy-path TRC10 transfer that reads the stale `asset` balance and restores/creates a matching `assetV2` balance — duplicating tokens that already exist elsewhere. This is unbacked-balance creation of a live asset, a direct funds-integrity violation.

### Likelihood Explanation
Exploitability is gated entirely on `DynamicPropertiesStore.getAllowSameTokenName() == 0`, the legacy chain-wide flag that once compatibility toggles to `1` (as it has for a long time on TRON mainnet) permanently disables the `asset`-map branch in `reduceAssetAmountV2`/`addAssetAmountV2`, closing this path. I could not verify from static code alone whether this flag is still `0` (or settable back to `0`) in the target deployment/genesis configuration; if the network being assessed already has `AllowSameTokenName == 1` locked in, this specific analog is not currently reachable, which is an important caveat.

### Recommendation
Remove the duplicate legacy `asset` map handling from `reduceAssetAmountV2`/`addAssetAmountV2` (mirroring the report's advice to eliminate duplicate `balances`/`allowed` logic), or at minimum make `MUtil.transferAllToken` (and any other code path that mutates `assetV2`) also update the legacy `asset` map so both mappings stay consistent, and stop treating the legacy map as an authoritative balance source when `assetV2` exists.

### Proof of Concept
1. With `AllowSameTokenName == 0`, deploy a contract `C` and give it TRC10 token `T` balance `N` (both `asset[T]=N` and `assetV2[T]=N` populated via `AssetIssueActuator`/`TransferAssetActuator`).
2. Trigger `SELFDESTRUCT` inside `C`, forwarding to beneficiary `B` — `MUtil.transferAllToken` sets `C.assetV2[T]=0`, `B.assetV2[T]+=N`, but `C.asset[T]` remains `N`.
3. Recreate account `C` (or reuse its address per CREATE2/contract-account reuse rules) and issue a legacy `TransferAssetContract` moving a small amount `k <= N` of token `T` out of `C`.
4. `reduceAssetAmountV2` reads `currentAmount = C.asset[T] = N` (stale), passes the `k <= N` check, and rewrites `C.assetV2[T] = N - k` — resurrecting up to `N` units of `assetV2` balance on `C` that are not backed by any actual token supply, since the original `N` was already moved to `B` in step 2.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/utils/MUtil.java (L28-41)
```java
  public static void transferAllToken(Repository deposit, byte[] fromAddress, byte[] toAddress) {
    AccountCapsule fromAccountCap = deposit.getAccount(fromAddress);
    Protocol.Account.Builder fromBuilder = fromAccountCap.getInstance().toBuilder();
    AccountCapsule toAccountCap = deposit.getAccount(toAddress);
    toAccountCap.importAllAsset();
    Protocol.Account.Builder toBuilder = toAccountCap.getInstance().toBuilder();
    fromAccountCap.getAssetMapV2().forEach((tokenId, amount) -> {
      toBuilder.putAssetV2(tokenId, toBuilder.getAssetV2Map().getOrDefault(tokenId, 0L) + amount);
      fromBuilder.putAssetV2(tokenId, 0L);
    });

    deposit.putAccountValue(fromAddress, new AccountCapsule(fromBuilder.build()));
    deposit.putAccountValue(toAddress, new AccountCapsule(toBuilder.build()));
  }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/AccountCapsule.java (L780-813)
```java
  public boolean reduceAssetAmountV2(byte[] key, long amount,
                                     DynamicPropertiesStore dynamicPropertiesStore, AssetIssueStore assetIssueStore) {
    importAsset(key);
    //key is token name
    boolean disableJavaLangMath = dynamicPropertiesStore.disableJavaLangMath();
    if (dynamicPropertiesStore.getAllowSameTokenName() == 0) {
      Map<String, Long> assetMap = this.account.getAssetMap();
      AssetIssueCapsule assetIssueCapsule = assetIssueStore.get(key);
      String tokenID = assetIssueCapsule.getId();
      String nameKey = ByteArray.toStr(key);
      Long currentAmount = assetMap.get(nameKey);
      if (amount > 0 && null != currentAmount && amount <= currentAmount) {
        this.account = this.account.toBuilder()
                .putAsset(nameKey, subtractExact(currentAmount, amount, disableJavaLangMath))
                .putAssetV2(tokenID, subtractExact(currentAmount, amount, disableJavaLangMath))
                .build();
        return true;
      }
    }
    //key is token id
    if (dynamicPropertiesStore.getAllowSameTokenName() == 1) {
      String tokenID = ByteArray.toStr(key);
      Map<String, Long> assetMapV2 = this.account.getAssetV2Map();
      Long currentAmount = assetMapV2.get(tokenID);
      if (amount > 0 && null != currentAmount && amount <= currentAmount) {
        this.account = this.account.toBuilder()
                .putAssetV2(tokenID, subtractExact(currentAmount, amount, disableJavaLangMath))
                .build();
        return true;
      }
    }

    return false;
  }
```
