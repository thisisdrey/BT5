Based on my investigation, I found a directly analogous condition in `AssetIssueActuator`, which mirrors the OpenQ `mintBounty` frontrunning pattern: a user-supplied unique identifier (the asset `name`), checked for existence in `validate()` and then written using that same name as the store key in `execute()`, with no binding of the identifier to the submitter's address.

### Title
Asset issuance (`AssetIssueContract`) can be frontrun via attacker-chosen duplicate token name, causing legitimate issuer's transaction to permanently fail - ([File: actuator/src/main/java/org/tron/core/actuator/AssetIssueActuator.java])

### Summary
`AssetIssueActuator` lets any account submit an `AssetIssueContract` with an arbitrary, attacker-chosen `name` field. When `AllowSameTokenName` is disabled, uniqueness of the name is enforced only by a pre-execution check in `validate()`, and the winning transaction is whichever is included in a block first — an outcome fully controllable by an attacker who observes the victim's pending transaction in the mempool and resubmits an identical name with higher energy price/priority.

### Finding Description
In `validate()`, the actuator checks that no asset with the same name already exists: [1](#0-0) 

In `execute()`, the token is persisted keyed by that same user-supplied name via `assetIssueCapsule.createDbKey()`: [2](#0-1) 

Because the `name` is taken verbatim from the transaction and is visible in the mempool before block inclusion, an attacker can observe a pending `AssetIssueContract` transaction, copy its `name` field into their own transaction (with their own `ownerAddress`), and get it mined first (e.g., by paying more bandwidth/priority or via faster propagation). This is functionally identical to the OpenQ `mintBounty(bountyId)` frontrunning bug: the identifier that should uniquely belong to the legitimate submitter is takeable by any other account, and the legitimate submitter's transaction subsequently fails validation with `"Token exists"` — exactly as demonstrated in the existing regression test: [3](#0-2) 

### Impact Explanation
A successful frontrun causes a legitimate issuer's asset-issuance transaction to fail (`ContractValidateException: "Token exists"`), consuming the victim's bandwidth/fee for an already-broadcast (though rejected pre-execution, still costs propagation and possibly resubmission overhead) transaction and blocking them from ever issuing a token under that name unless they choose a different name. This is a targeted denial-of-service against an unprivileged "asset issuer" transaction — exactly the actor class in scope. It does not itself cause theft of funds, unauthorized asset transfer, or a node crash, so it is bounded to a functional/availability impact on the token-name namespace rather than a fund-theft or consensus-safety bug.

### Likelihood Explanation
Exploitation requires only mempool visibility (any public node) and the ability to submit a competing transaction with higher priority/energy or faster propagation, which is trivial for any network participant and requires no special privileges — matching the audit finding's low bar for exploitation. Note that this path is gated by the `AllowSameTokenName` dynamic parameter being `0`; when the parameter is `1` (name-uniqueness disabled, token IDs auto-incremented via `tokenIdNum` in `execute()`), the name-collision race does not apply. I could not fully confirm from the indexed code whether `AllowSameTokenName` is still settable to `0` on the current mainnet or is now permanently forced to `1` by a hardfork/proposal default — this affects whether the vulnerable code path is currently reachable in production.

### Recommendation
Do not enforce first-come-first-served uniqueness on a fully attacker-controlled string when `AllowSameTokenName == 0`. Options: (1) bind token identity to `(ownerAddress, name)` rather than `name` alone so it cannot be squatted by unrelated accounts, or (2) treat `name` purely as metadata and always key assets by the auto-incrementing `tokenIdNum` (as is already done for the V2 store), removing the race entirely — consistent with the `mintBounty` recommendation to derive the identifier from a salt plus `msg.sender` instead of accepting it directly as attacker-supplied input.

### Proof of Concept
1. Victim broadcasts `AssetIssueContract` with `name = "MYTOKEN"` and `ownerAddress = victim`.
2. Attacker observes this transaction in the mempool (or public API/broadcast channel) and immediately broadcasts their own `AssetIssueContract` with `name = "MYTOKEN"` and `ownerAddress = attacker`, using a higher priority/energy so it is more likely to be included first.
3. If the attacker's transaction lands in a block before the victim's, `AssetIssueStore` now contains `"MYTOKEN"` owned by the attacker.
4. When the victim's transaction executes, `validate()` finds `assetIssueStore.get("MYTOKEN".toByteArray()) != null` and throws `ContractValidateException("Token exists")`, exactly as reproduced in `IssueSameTokenNameAssert`: [4](#0-3)

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/AssetIssueActuator.java (L78-87)
```java
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

**File:** actuator/src/main/java/org/tron/core/actuator/AssetIssueActuator.java (L210-214)
```java
    if (dynamicStore.getAllowSameTokenName() == 0
        && assetIssueStore.get(assetIssueContract.getName().toByteArray())
        != null) {
      throw new ContractValidateException("Token exists");
    }
```

**File:** framework/src/test/java/org/tron/core/actuator/AssetIssueActuatorTest.java (L1700-1731)
```java
  @Test
  public void IssueSameTokenNameAssert() {
    dbManager.getDynamicPropertiesStore().saveAllowSameTokenName(0);
    String ownerAddress = "418beaa1a8e2d45367af7bae7c49009876a4fa4301";

    long id = dbManager.getDynamicPropertiesStore().getTokenIdNum() + 1;
    dbManager.getDynamicPropertiesStore().saveTokenIdNum(id);
    AssetIssueContract assetIssueContract = AssetIssueContract.newBuilder()
        .setOwnerAddress(ByteString.copyFrom(ByteArray.fromHexString(ownerAddress)))
        .setName(ByteString.copyFrom(ByteArray.fromString(NAME))).setId(Long.toString(id))
        .setTotalSupply(TOTAL_SUPPLY)
        .setTrxNum(TRX_NUM).setNum(NUM).setStartTime(1).setEndTime(100).setVoteScore(2)
        .setDescription(ByteString.copyFrom(ByteArray.fromString(DESCRIPTION)))
        .setUrl(ByteString.copyFrom(ByteArray.fromString(URL))).build();
    AssetIssueCapsule assetIssueCapsule = new AssetIssueCapsule(assetIssueContract);
    dbManager.getAssetIssueStore().put(assetIssueCapsule.createDbKey(), assetIssueCapsule);

    AccountCapsule ownerCapsule = new AccountCapsule(
        ByteString.copyFrom(ByteArray.fromHexString(ownerAddress)),
        ByteString.copyFromUtf8("owner11"), AccountType.AssetIssue);
    ownerCapsule.addAsset(NAME.getBytes(), 1000L);
    dbManager.getAccountStore().put(ownerCapsule.getAddress().toByteArray(), ownerCapsule);

    AssetIssueActuator actuator = new AssetIssueActuator();
    actuator.setChainBaseManager(dbManager.getChainBaseManager()).setAny(getContract());

    TransactionResultCapsule ret = new TransactionResultCapsule();
    Long blackholeBalance = dbManager.getAccountStore().getBlackhole().getBalance();
    // SameTokenName not active, same assert name, should failure

    processAndCheckInvalid(actuator, ret, "Token exists", "Token exists");

```
