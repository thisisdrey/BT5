### Title
Permissionless duplicate token-name issuance in `AssetIssueActuator` enables spoofed "official" assets - (File: `actuator/src/main/java/org/tron/core/actuator/AssetIssueActuator.java`)

### Summary
`AssetIssueActuator` lets any signed account create a new TRC-10 token via `AssetIssueContract`, obtaining a globally unique numeric `id` from the shared `TokenIdNum` counter, but the human-readable `name`/`abbr` fields used by wallets, exchanges, and users to identify a token are only checked for uniqueness when the chain parameter `AllowSameTokenName` is `0`. Once that parameter is `1` (the long-standing mainnet/production setting, controlled by an on-chain committee proposal), any unprivileged account can issue a token whose `name` collides with an existing, well-known/official token, and the actuator will happily accept it as a new token under a fresh `id`.

### Finding Description
In `validate()`, the uniqueness check is gated entirely behind `getAllowSameTokenName() == 0`: [1](#0-0) 

When `AllowSameTokenName` is `1`, this check is skipped completely, so `execute()` proceeds to allocate a new sequential id from `dynamicStore.getTokenIdNum()` and persist the asset regardless of whether an identical `name` already exists: [2](#0-1) 

The only name restriction that always applies is a block on the literal string `"trx"`; no other reserved/official names are protected: [3](#0-2) 

This mirrors the external report's bug class: a global, unguarded counter (`TokenIdNum`/`OffRampCounter`) is used to mint new "instances" (tokens/off-ramp states) under a shared authority (the TRC-10 asset registry/Program ID), with no allowlist or check tying the human-facing identifier (`name`/`admin`) to an authorized/official issuer. Downstream query paths such as `Wallet.getAssetIssueByName` explicitly acknowledge and handle the "multiple assets share a name" case rather than rejecting it, confirming that name collisions are a supported, unguarded state: [4](#0-3) 

### Impact Explanation
An unprivileged attacker can issue a TRC-10 token with a `name`/`abbr` identical to an existing high-profile or "official" token, then market it (via exchanges, DApps, or social channels) as the legitimate asset. Because wallets, exchange listings, and users commonly identify TRC-10 tokens by `name` rather than the opaque numeric `id`, this enables classic token-spoofing/name-squatting scams: victims can be tricked into acquiring or trading the fake token believing it is the official one, leading to financial loss. This is a Medium-severity, permission-model/spoofing issue reachable by any asset issuer.

### Likelihood Explanation
Likelihood is high: this requires only a single signed `AssetIssueContract` transaction from any funded account (paying the standard `AssetIssueFee`), no special privileges, and `AllowSameTokenName` has been the committee-approved production value on java-tron networks for a long time, so the uniqueness guard is effectively disabled in practice.

### Recommendation
Reintroduce a uniqueness/reservation mechanism for asset `name`/`abbr` regardless of `AllowSameTokenName`, or add an explicit allowlist/registry (analogous to the `GlobalConfig`/`authorized_initializer` mitigation in the report) so that reissuing a `name` that already maps to an existing asset either fails or requires proof of control (e.g., only the original issuer's account, or a committee-approved allowlist) before a new `AssetIssueContract` with a colliding `name` can be accepted in `AssetIssueActuator.validate()`.

### Proof of Concept
1. Ensure `AllowSameTokenName == 1` (current production default, set via committee proposal `ALLOW_SAME_TOKEN_NAME`).
2. Attacker account `A` (unrelated to any official issuer) broadcasts an `AssetIssueContract` with `name = "USDT"` (or any other well-known token's exact name) and arbitrary `totalSupply`/fee parameters.
3. `AssetIssueActuator.validate()` skips the "Token exists" check (only entered when `AllowSameTokenName == 0`), and `execute()` allocates a new `tokenIdNum`, persists the asset into `AssetIssueV2Store`, and credits the attacker's account with the full supply under the spoofed name.
4. The attacker lists/promotes this token as the "official" asset; victims interacting by `name` (e.g., via `Wallet.getAssetIssueByName`) receive it alongside (or instead of) the legitimate token, enabling scams.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/AssetIssueActuator.java (L70-87)
```java
      AssetIssueCapsule assetIssueCapsule = new AssetIssueCapsule(assetIssueContract);
      AssetIssueCapsule assetIssueCapsuleV2 = new AssetIssueCapsule(assetIssueContract);
      long tokenIdNum = dynamicStore.getTokenIdNum();
      tokenIdNum++;
      assetIssueCapsule.setId(Long.toString(tokenIdNum));
      assetIssueCapsuleV2.setId(Long.toString(tokenIdNum));
      dynamicStore.saveTokenIdNum(tokenIdNum);

      if (dynamicStore.getAllowSameTokenName() == 0) {
        assetIssueCapsuleV2.setPrecision(0);
        assetIssueStore
            .put(assetIssueCapsule.createDbKey(), assetIssueCapsule);
        assetIssueV2Store
            .put(assetIssueCapsuleV2.createDbV2Key(), assetIssueCapsuleV2);
      } else {
        assetIssueV2Store
            .put(assetIssueCapsuleV2.createDbV2Key(), assetIssueCapsuleV2);
      }
```

**File:** actuator/src/main/java/org/tron/core/actuator/AssetIssueActuator.java (L169-174)
```java
    if (dynamicStore.getAllowSameTokenName() != 0) {
      String name = assetIssueContract.getName().toStringUtf8().toLowerCase(Locale.ROOT);
      if (("trx").equals(name)) {
        throw new ContractValidateException("assetName can't be trx");
      }
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/AssetIssueActuator.java (L210-214)
```java
    if (dynamicStore.getAllowSameTokenName() == 0
        && assetIssueStore.get(assetIssueContract.getName().toByteArray())
        != null) {
      throw new ContractValidateException("Token exists");
    }
```

**File:** framework/src/main/java/org/tron/core/Wallet.java (L1724-1742)
```java
    } else {
      // get asset issue by name from new DB
      List<AssetIssueCapsule> assetIssueCapsuleList =
          chainBaseManager.getAssetIssueV2Store().getAllAssetIssues();
      AssetIssueList.Builder builder = AssetIssueList.newBuilder();
      assetIssueCapsuleList
          .stream()
          .filter(assetIssueCapsule -> assetIssueCapsule.getName().equals(assetName))
          .forEach(
              issueCapsule -> {
                processor.updateUsage(issueCapsule);
                builder.addAssetIssue(issueCapsule.getInstance());
              });

      // check count
      if (builder.getAssetIssueCount() > 1) {
        throw new NonUniqueObjectException(
            "To get more than one asset, please use getAssetIssueById syntax");
      } else {
```
