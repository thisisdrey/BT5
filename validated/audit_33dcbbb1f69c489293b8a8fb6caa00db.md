### Title
SUICIDE (SELFDESTRUCT) opcode charges fixed energy but internally loops over an unbounded TRC10 asset map, allowing a cheap opcode to perform unbounded work - (File: actuator/src/main/java/org/tron/core/vm/utils/MUtil.java)

### Summary
`MUtil.transferAllToken()`, invoked from `Program.suicide()` when a contract self-destructs, iterates over the *entire* `AssetMapV2` of the destructing contract account to move every TRC10 token balance to the beneficiary. The number of distinct TRC10 tokens (`assetMapV2` entries) an account can hold is not capped — any address can send arbitrary TRC10 tokens to a contract via `TransferAssetActuator`, growing the map indefinitely. Yet the `SUICIDE`/`SUICIDE_V2` opcode charges a fixed energy cost (`SUICIDE = 0`, `SUICIDE_V2 = 5000` in `EnergyCost.java`), independent of how many token entries must be processed. This mirrors the Sherlock finding for `sweepTo()`: an unbounded, user-inflatable collection is iterated inside a state-transition path whose gas/energy accounting does not scale with the collection size.

### Finding Description
- `Program.suicide()` (actuator/src/main/java/org/tron/core/vm/program/Program.java:451-517) calls `MUtil.transferAllToken(getContractState(), owner, obtainer)` when `VMConfig.allowTvmTransferTrc10()` is enabled, in order to move all TRC10 balances from the self-destructing account to the beneficiary.
- `transferAllToken()` (actuator/src/main/java/org/tron/core/vm/utils/MUtil.java:28-41) calls `fromAccountCap.getAssetMapV2().forEach(...)`, iterating every token entry the account holds and writing an updated value into both the `from` and `to` account protobuf builders.
- `AccountCapsule.getAssetMapV2()` (chainbase/src/main/java/org/tron/core/capsule/AccountCapsule.java:878-885) calls `importAllAsset()`, which (when asset optimization is enabled) pulls in *all* asset entries for the account from `AccountAssetStore.getAllAssets()` (chainbase/src/main/java/org/tron/core/store/AccountAssetStore.java:97-109) — a full prefix scan over the account's asset keyspace.
- There is no cap on the number of distinct TRC10 tokens (`assetV2Map` size) an account can accumulate. `TransferAssetActuator.validate()`/`execute()` only checks amount/balance validity, not the number of distinct assets already held by the recipient — any address (including a smart contract) can be sent an arbitrary number of different TRC10 tokens by third parties.
- The `SUICIDE` opcode's energy cost is a constant defined in `EnergyCost.java` (`SUICIDE = 0`, `SUICIDE_V2 = 5000`), not scaled by the number of assets that must be swept.

### Impact Explanation
An attacker can pre-fund a target contract with a large number of distinct TRC10 token types (each transfer is cheap and can be sent by unrelated addresses) to inflate its `assetV2Map`. When that contract (or any contract, including one the attacker controls) later executes `SELFDESTRUCT`, `transferAllToken()` must iterate/scan and rewrite every one of those asset entries while the opcode itself is charged a small fixed energy amount. This can cause:
- Execution time far exceeding what the fixed energy charge represents, creating a CPU/latency amplification vector for block producers (a form of computational DoS within a single transaction that is priced far below its actual cost), and
- Because per-token processing cost is not linearly priced into `SUICIDE`'s energy, an attacker can make self-destruct calls artificially expensive to execute while cheap to pay for, degrading node processing time under adversarial resource growth of a target account's asset map.

This does not directly cause token loss (the tokens are still correctly transferred), so the primary impact is resource-exhaustion / underpriced-computation rather than fund theft, which aligns with the medium severity of the original sweepTo() finding (gas/energy insufficiency for unbounded collection iteration).

### Likelihood Explanation
Likelihood is moderate: any unprivileged account can call `TransferAssetContract` to send many distinct TRC10 tokens to a target contract address cheaply (asset issuance is more restricted, but an attacker controlling several previously issued TRC10 tokens, or coordinating with others, can inflate a victim/self-owned contract's asset map over time). Triggering `SELFDESTRUCT` on that contract is a standard, permissionless operation reachable from any transaction that invokes the contract's self-destruct path.

### Recommendation
- Cap the number of distinct TRC10 asset types (`assetV2Map`/`AccountAssetStore` prefix scan size) an account is allowed to accumulate, similar to how `FrozenSupply` count is capped via `dynamicStore.getMaxFrozenSupplyNumber()` (actuator/src/main/java/org/tron/core/actuator/AssetIssueActuator.java:232-234).
- Alternatively, make the `SUICIDE`/`SUICIDE_V2` energy cost scale with the number of asset entries being transferred in `transferAllToken()`, so the caller pays for the actual work performed.

### Proof of Concept
1. Deploy a contract `C` with a working `SELFDESTRUCT`/self-destruct function, `allowTvmTransferTrc10()` enabled.
2. Issue (or acquire) N distinct TRC10 tokens and, from N different unprivileged accounts, call `TransferAssetContract` to send small amounts of each of the N tokens to `C`'s address, growing `C`'s `assetV2Map` to size N with no on-chain cap.
3. Call `C`'s function that executes `SELFDESTRUCT`. The VM charges the fixed `SUICIDE_V2` energy cost (5000, per `EnergyCost.java:58`), but `Program.suicide()` → `MUtil.transferAllToken()` (actuator/src/main/java/org/tron/core/vm/utils/MUtil.java:28-41) internally iterates and rewrites all N asset entries via `getAssetMapV2()`/`importAllAsset()`/`AccountAssetStore.getAllAssets()` (chainbase/src/main/java/org/tron/core/store/AccountAssetStore.java:97-109), performing O(N) work priced as O(1) energy. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

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

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L478-488)
```java
      if (VMConfig.allowTvmTransferTrc10()) {
        getContractState().addBalance(blackHoleAddress, balance);
        MUtil.transferAllToken(getContractState(), owner, blackHoleAddress);
      }
    } else {
      createAccountIfNotExist(getContractState(), obtainer);
      try {
        MUtil.transfer(getContractState(), owner, obtainer, balance);
        if (VMConfig.allowTvmTransferTrc10()) {
          MUtil.transferAllToken(getContractState(), owner, obtainer);
        }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/AccountCapsule.java (L878-885)
```java
  public Map<String, Long> getAssetMapV2() {
    importAllAsset();
    Map<String, Long> assetMap = this.account.getAssetV2Map();
    if (assetMap.isEmpty()) {
      assetMap = Maps.newHashMap();
    }
    return assetMap;
  }
```

**File:** chainbase/src/main/java/org/tron/core/store/AccountAssetStore.java (L97-109)
```java
  public Map<String, Long> getAllAssets(Protocol.Account account) {
    Map<String, Long> assets = new HashMap<>();
    if (account.getAssetOptimized()) {
      Map<WrappedByteArray, byte[]> map = prefixQuery(account.getAddress().toByteArray());
      map.forEach((k, v) -> {
        byte[] assetID = ByteArray.subArray(k.getBytes(),
                account.getAddress().toByteArray().length, k.getBytes().length);
        assets.put(ByteArray.toStr(assetID), Longs.fromByteArray(v));
      });
    }
    account.getAssetV2Map().forEach((k, v) -> assets.put(k, v));
    return assets;
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/EnergyCost.java (L57-58)
```java
  private static final long SUICIDE = 0;
  private static final long SUICIDE_V2 = 5000;
```
