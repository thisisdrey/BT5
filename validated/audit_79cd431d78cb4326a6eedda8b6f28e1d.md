Found it: `ParticipateAssetIssueActuator.java` contains exactly the cache-then-overwrite pattern described in the report, and unlike `TransferActuator`/`TransferAssetActuator`/`DelegateResourceActuator`, it has **no check that `ownerAddress != toAddress`**.

### Title
Fund duplication via self-participation in ParticipateAssetIssueActuator (owner == to_address) - (File: actuator/src/main/java/org/tron/core/actuator/ParticipateAssetIssueActuator.java)

### Summary
`ParticipateAssetIssueContract` lets any account "buy" a TRC10 asset from an issuer by paying TRX (`amount`/`cost`) to `to_address` and receiving the asset in return. The actuator fetches both the `ownerAccount` and `toAccount` capsules independently and mutates each one's cached in-memory object using stale/absolute values instead of atomic increments applied to a single shared state, and never validates that `ownerAddress != toAddress`.

### Finding Description
In `execute()`: [1](#0-0) 
the code does:
1. `ownerAccount = accountStore.get(ownerAddress)`, then `ownerAccount.setBalance(balance - cost - fee)` and `ownerAccount.addAssetAmountV2(key, exchangeAmount, ...)`.
2. `toAccount = accountStore.get(toAddress)`, then `toAccount.setBalance(addExact(toAccount.getBalance(), cost))` and `toAccount.reduceAssetAmountV2(key, exchangeAmount, ...)`.
3. Finally both `ownerAccount` and `toAccount` are written back with `accountStore.put(...)`.

If `ownerAddress == toAddress` (the caller participates in their own asset issuance, i.e. buys their own token with their own TRX), `accountStore.get(ownerAddress)` and `accountStore.get(toAddress)` return **two separate `AccountCapsule` objects**, each independently loaded from the store with the account's original balance/asset amount. The two objects are mutated independently in memory (one via `setBalance(balance-cost-fee)`, the other via `setBalance(addExact(oldBalance, cost))`) and only the *last* `accountStore.put()` call actually persists. Because `toAccount.put()` runs after `ownerAccount.put()` for the same DB key, `toAccount`'s state (computed from the stale, pre-transaction balance) overwrites `ownerAccount`'s update. This is the identical bug class as the `ERC20Rebasing._transfer` self-transfer report: independently-cached before/after states for `from`/`to` collapse into one when `from == to`, and the write order determines which side "wins," producing incorrect balances rather than a neutral no-op.

The `validate()` function checks that `ownerAccount` has an asset issuer relationship, sufficient balance, and various other constraints, but at no point compares `ownerAddress` to `toAddress`: [2](#0-1) 
This is unlike the sibling actuators which explicitly forbid self-transfer/self-delegation: [3](#0-2) [4](#0-3) [5](#0-4) 

Normally `to_address` in `ParticipateAssetIssueContract` is the asset issuer, and `owner_address` is the buyer — these are typically different accounts. However, nothing in `validate()` prevents an asset issuer from calling `ParticipateAssetIssueContract` on their own issued asset (`owner_address == to_address == issuer`), which is a fully legitimate, unprivileged, single-transaction scenario reachable by any account that has issued a TRC10 token.

### Impact Explanation
When the issuer self-participates (`owner_address == to_address`), depending on `cost`/`exchangeAmount` values the final persisted account state can reflect only one side of the transaction (e.g., TRX balance increased by `cost` without the corresponding deduction actually sticking, or vice versa, and/or the asset amount ends up either duplicated or halved relative to correct accounting), since two independently-mutated in-memory snapshots of the same account race to overwrite the same DB key. This can create an unbacked TRX balance or unbacked asset balance for the issuer — a concrete "unbacked balance" / fund-duplication condition.

### Likelihood Explanation
High reachability: any account that has issued a TRC10 asset (`AssetIssueContract`, permissionless) can subsequently broadcast a single `ParticipateAssetIssueContract` transaction with `owner_address` and `to_address` both set to itself. No special privilege, no witness/SR role, and no additional preconditions beyond normal asset-issuance and having sufficient TRX are required.

### Recommendation
In `ParticipateAssetIssueActuator.validate()`, add an explicit check rejecting `Arrays.equals(ownerAddress, toAddress)` (mirroring the checks already present in `TransferActuator`, `TransferAssetActuator`, and `DelegateResourceActuator`), or refactor `execute()` to operate on a single shared `AccountCapsule` instance (fetch once when `owner==to`) and apply relative (add/subtract) balance and asset adjustments rather than mutating two independently-loaded snapshots.

### Proof of Concept
1. Attacker issues a TRC10 asset `X` via `AssetIssueContract` from address `A` (attacker-controlled), setting `trxNum`/`num` favorably.
2. Attacker broadcasts `ParticipateAssetIssueContract` with `owner_address = A`, `to_address = A`, `asset_name = X`, `amount = cost`.
3. `ParticipateAssetIssueActuator.execute()` loads two separate `AccountCapsule` instances for `A` (once as owner, once as `toAccount`), mutates each independently based on `A`'s pre-transaction balance/asset amount, then persists both — the later `accountStore.put(toAddress, toAccount)` call overwrites the earlier owner-side update for the same address, leaving `A`'s final balance/asset state inconsistent with a correct paired debit/credit, producing an unbacked increase in TRX balance or asset holdings for `A`. [6](#0-5)

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ParticipateAssetIssueActuator.java (L48-99)
```java
  public boolean execute(Object object) throws ContractExeException {
    TransactionResultCapsule ret = (TransactionResultCapsule) object;
    if (Objects.isNull(ret)) {
      throw new RuntimeException(ActuatorConstant.TX_RESULT_NULL);
    }

    long fee = calcFee();
    AccountStore accountStore = chainBaseManager.getAccountStore();
    DynamicPropertiesStore dynamicStore = chainBaseManager.getDynamicPropertiesStore();
    AssetIssueStore assetIssueStore = chainBaseManager.getAssetIssueStore();
    AssetIssueV2Store assetIssueV2Store = chainBaseManager.getAssetIssueV2Store();
    try {
      final ParticipateAssetIssueContract participateAssetIssueContract =
          any.unpack(ParticipateAssetIssueContract.class);
      long cost = participateAssetIssueContract.getAmount();

      //subtract from owner address
      byte[] ownerAddress = participateAssetIssueContract.getOwnerAddress().toByteArray();
      AccountCapsule ownerAccount = accountStore.get(ownerAddress);
      long balance = subtractExact(ownerAccount.getBalance(), cost);
      balance = subtractExact(balance, fee);
      ownerAccount.setBalance(balance);
      byte[] key = participateAssetIssueContract.getAssetName().toByteArray();

      //calculate the exchange amount
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

    return true;
```

**File:** actuator/src/main/java/org/tron/core/actuator/ParticipateAssetIssueActuator.java (L102-132)
```java
  @Override
  public boolean validate() throws ContractValidateException {
    if (this.any == null) {
      throw new ContractValidateException(ActuatorConstant.CONTRACT_NOT_EXIST);
    }
    if (chainBaseManager == null) {
      throw new ContractValidateException(ActuatorConstant.STORE_NOT_EXIST);
    }
    AccountStore accountStore = chainBaseManager.getAccountStore();
    DynamicPropertiesStore dynamicStore = chainBaseManager.getDynamicPropertiesStore();
    AssetIssueStore assetIssueStore = chainBaseManager.getAssetIssueStore();
    AssetIssueV2Store assetIssueV2Store = chainBaseManager.getAssetIssueV2Store();
    if (!this.any.is(ParticipateAssetIssueContract.class)) {
      throw new ContractValidateException(
          "contract type error,expected type [ParticipateAssetIssueContract],real type[" + any
              .getClass() + "]");
    }

    final ParticipateAssetIssueContract participateAssetIssueContract;
    try {
      participateAssetIssueContract =
          this.any.unpack(ParticipateAssetIssueContract.class);
    } catch (InvalidProtocolBufferException e) {
      logger.debug(e.getMessage(), e);
      throw new ContractValidateException(e.getMessage());
    }
    //Parameters check
    byte[] ownerAddress = participateAssetIssueContract.getOwnerAddress().toByteArray();
    byte[] toAddress = participateAssetIssueContract.getToAddress().toByteArray();
    byte[] assetName = participateAssetIssueContract.getAssetName().toByteArray();
    long amount = participateAssetIssueContract.getAmount();
```

**File:** actuator/src/main/java/org/tron/core/actuator/TransferActuator.java (L111-113)
```java
    if (Arrays.equals(toAddress, ownerAddress)) {
      throw new ContractValidateException("Cannot transfer TRX to yourself.");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/TransferAssetActuator.java (L147-149)
```java
    if (Arrays.equals(ownerAddress, toAddress)) {
      throw new ContractValidateException("Cannot transfer asset to yourself.");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/DelegateResourceActuator.java (L198-201)
```java
    if (Arrays.equals(receiverAddress, ownerAddress)) {
      throw new ContractValidateException(
          "receiverAddress must not be the same as ownerAddress");
    }
```
