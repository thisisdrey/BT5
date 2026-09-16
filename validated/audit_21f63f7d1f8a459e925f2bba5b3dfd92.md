### Title
Active-permission holder can unilaterally overwrite Owner permission via `AccountPermissionUpdateContract`, causing account takeover - (File: actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java)

### Summary
`AccountPermissionUpdateActuator` never requires that a transaction which *replaces* an account's `Owner` permission be authorized under the account's current `Owner` threshold. Authorization for this actuator is resolved the same way as any other contract: via `TransactionCapsule.validateSignature()`/`checkPermission()`, which looks up whichever permission the signer selects (`contract.getPermissionId()`) and only checks that permission's own threshold. Because the `Active` permission's allowed-operations bitmap is permitted (and validated) to include `ContractType.AccountPermissionUpdateContract` (bit 30, part of `getAvailableContractType()`), an account owner who delegates that bit to a lower-trust `Active` key (e.g. a "hot" operational key meant only for transfers/voting) gives that key the unilateral power to replace the `Owner` permission entirely — with a completely different key set and a threshold the delegated key alone can satisfy.

### Finding Description
`checkPermission()` in `AccountPermissionUpdateActuator` validates the *new* `Active` permission's `operations` bitmap only against `dynamicStore.getAvailableContractType()`, which includes `AccountPermissionUpdateContract`: [1](#0-0) 

This is confirmed by the test that documents the exact bitmap contents of `getAvailableContractType()` versus the more restrictive `getActiveDefaultOperations()` (which is the *default* template only, not an enforced upper bound): [2](#0-1) [3](#0-2) 

Once an `Active` permission has that bit set, any transaction of type `AccountPermissionUpdateContract` signed under that `Active` permission (`contract.getPermissionId()` pointing at the active permission's id) is authorized purely by `TransactionCapsule.checkPermission`/`checkWeight`, which only checks the *Active* permission's own (lower) threshold and its `operations` bit — with no additional requirement that the *Owner* permission's threshold also be met: [4](#0-3) [5](#0-4) 

`AccountPermissionUpdateActuator.execute()` then unconditionally overwrites `Owner`, `Witness`, and all `Active` permissions with whatever was supplied in the contract, with no comparison to the pre-existing `Owner` permission or requirement that current owner-key holders co-sign: [6](#0-5) [7](#0-6) 

The net effect: the intra-account privilege hierarchy (Owner > Active) is not actually enforced when the sensitive operation is "change the permission hierarchy itself" — an `Active` key that was only meant to be trusted for a narrow set of operations, once granted the `AccountPermissionUpdateContract` bit, becomes fully equivalent to (or more powerful than) the `Owner` key, because it can install brand-new owner keys/thresholds unilaterally. This mirrors the XXL-Job class of bug (CWE-863: Incorrect Authorization) where a lower-privileged principal can perform an operation reserved for another/higher-privileged principal because the authorization check validates only the acting principal's own token/permission rather than the resource's actual required authorization level.

### Impact Explanation
Any wallet/service/application that follows TRON's documented multi-sig delegation model and grants an `Active` key the `AccountPermissionUpdateContract` operation (a legitimate, supported configuration) is exposed to full account takeover by that single Active key: it can replace the `Owner` permission's keys and threshold in one transaction, permanently locking out the legitimate owner and any co-signers, and gaining unilateral control of the account's balance, TRC10/TRC20 assets, resource delegation, and voting rights. This is a concrete, permanent, unauthorized account-control outcome (High severity), directly reachable by a single signed transaction from any holder of an Active key configured with that bit.

### Likelihood Explanation
Requires the account owner to have granted the `Active` permission the `AccountPermissionUpdateContract` operation bit (a valid, non-default but explicitly supported configuration path, since `getAvailableContractType()` permits it and the actuator's own validation accepts it without additional restriction or warning). Given this bit is not blocked, disabled, or specially gated compared to any other bit in the 256-bit operations mask, and no additional runtime check re-verifies the true Owner threshold before applying the change, exploitation by a co-signer/insider or a compromised Active-key holder is straightforward once that delegation exists.

### Recommendation
In `AccountPermissionUpdateActuator.validate()`/`checkPermission()`, either (a) forbid setting the `AccountPermissionUpdateContract` bit in any `Active` permission's `operations` mask entirely (excluding it from `getAvailableContractType()` allowed bits for Active use, similar to how `ClearABIContract`/`UpdateBrokerageContract` are already excluded), or (b) require that any transaction which modifies `Owner`/`Witness`/`Active` permissions always be authorized under the account's *current* `Owner` permission threshold regardless of which `permissionId` was used to sign, so a delegated Active key can never unilaterally redefine the account's top-level authorization hierarchy.

### Proof of Concept
1. Owner account `A` creates an `Active` permission (id=2, threshold=1) for key `K`, and sets its `operations` bitmap bit for `ContractType.AccountPermissionUpdateContract` (bit index 30) — a configuration the actuator fully accepts per `checkPermission()`'s bitmap check against `getAvailableContractType()`.
2. Attacker/insider holding only private key `K` crafts an `AccountPermissionUpdateContract` transaction for account `A`, setting `contract.getPermissionId() = 2` (the Active permission), and supplies a brand-new `Owner` permission consisting solely of a key they control with threshold 1.
3. Signs the transaction with only `K`. `TransactionCapsule.validateSignature()` resolves permission id 2 (`Active`), confirms the `AccountPermissionUpdateContract` operation bit is set, and checks weight against the Active permission's threshold (1) — satisfied by `K` alone.
4. `AccountPermissionUpdateActuator.execute()` overwrites `A`'s `Owner` permission with the attacker-supplied permission (`AccountCapsule.updatePermissions`), completing takeover — the original owner key(s) no longer control the account.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L34-69)
```java
  @Override
  public boolean execute(Object object) throws ContractExeException {
    TransactionResultCapsule result = (TransactionResultCapsule) object;
    if (Objects.isNull(result)) {
      throw new RuntimeException(ActuatorConstant.TX_RESULT_NULL);
    }

    AccountStore accountStore = chainBaseManager.getAccountStore();
    long fee = calcFee();
    final AccountPermissionUpdateContract accountPermissionUpdateContract;
    try {
      accountPermissionUpdateContract = any.unpack(AccountPermissionUpdateContract.class);

      byte[] ownerAddress = accountPermissionUpdateContract.getOwnerAddress().toByteArray();
      AccountCapsule account = accountStore.get(ownerAddress);
      account.updatePermissions(accountPermissionUpdateContract.getOwner(),
          accountPermissionUpdateContract.getWitness(),
          accountPermissionUpdateContract.getActivesList());
      accountStore.put(ownerAddress, account);

      adjustBalance(accountStore, ownerAddress, -fee);
      if (chainBaseManager.getDynamicPropertiesStore().supportBlackHoleOptimization()) {
        chainBaseManager.getDynamicPropertiesStore().burnTrx(fee);
      } else {
        adjustBalance(accountStore, accountStore.getBlackhole(), fee);
      }

      result.setStatus(fee, code.SUCESS);
    } catch (BalanceInsufficientException | InvalidProtocolBufferException e) {
      logger.debug(e.getMessage(), e);
      result.setStatus(fee, code.FAILED);
      throw new ContractExeException(e.getMessage());
    }

    return true;
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L132-145)
```java
    //check operations
    if (operations.isEmpty() || operations.size() != 32) {
      throw new ContractValidateException("operations size must 32");
    }

    byte[] types1 = dynamicStore.getAvailableContractType();
    for (int i = 0; i < 256; i++) {
      boolean b = (operations.byteAt(i / 8) & (1 << (i % 8))) != 0;
      boolean t = ((types1[(i / 8)] & 0xff) & (1 << (i % 8))) != 0;
      if (b && !t) {
        throw new ContractValidateException(i + " isn't a validate ContractType");
      }
    }
    return true;
```

**File:** framework/src/test/java/org/tron/core/actuator/AccountPermissionUpdateActuatorTest.java (L919-947)
```java
  @Test
  public void checkAvailableContractTypeCorrespondingToCode() {
    // note: The aim of this test case is to show how the current codes work.
    // The default value is
    // 7fff1fc0037e0000000000000000000000000000000000000000000000000000,
    // and it should call the addSystemContractAndSetPermission to add new contract
    // type
    // When you add a new contact, you can add it to contractType,
    // as '|| contractType = ContractType.XXX',
    // and you will get the value from the output,
    // then update the value to checkAvailableContractType
    // and checkActiveDefaultOperations
    String validContractType = "7fff1fc0037ef80f000000000000000000000000000000000000000000000000";

    byte[] availableContractType = new byte[32];
    for (ContractType contractType : ContractType.values()) {
      if (contractType == org.tron.protos.Protocol.Transaction.Contract.ContractType.UNRECOGNIZED
          || contractType == ContractType.ClearABIContract
          || contractType == ContractType.UpdateBrokerageContract) {
        continue;
      }
      int id = contractType.getNumber();
      System.out.println("id is " + id);
      availableContractType[id / 8] |= (1 << id % 8);
    }

    System.out.println(ByteArray.toHexString(availableContractType));

    Assert.assertEquals(validContractType, ByteArray.toHexString(availableContractType));
```

**File:** framework/src/test/java/org/tron/core/actuator/AccountPermissionUpdateActuatorTest.java (L951-977)
```java
  @Test
  public void checkActiveDefaultOperationsCorrespondingToCode() {
    // note: The aim of this test case is to show how the current codes work.
    // The default value is
    // 7fff1fc0033e0000000000000000000000000000000000000000000000000000,
    // and it should call the addSystemContractAndSetPermission to add new contract
    // type
    String validContractType = "7fff1fc0033ef80f000000000000000000000000000000000000000000000000";

    byte[] availableContractType = new byte[32];
    for (ContractType contractType : ContractType.values()) {
      if (contractType == org.tron.protos.Protocol.Transaction.Contract.ContractType.UNRECOGNIZED
          || contractType == ContractType.AccountPermissionUpdateContract
          || contractType == ContractType.ClearABIContract
          || contractType == ContractType.UpdateBrokerageContract) {
        continue;
      }
      int id = contractType.getNumber();
      System.out.println("id is " + id);
      availableContractType[id / 8] |= (1 << id % 8);
    }

    System.out.println(ByteArray.toHexString(availableContractType));

    Assert.assertEquals(validContractType, ByteArray.toHexString(availableContractType));

  }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/TransactionCapsule.java (L468-496)
```java
  public static boolean validateSignature(Transaction transaction,
      byte[] hash, AccountStore accountStore, DynamicPropertiesStore dynamicPropertiesStore)
      throws PermissionException, SignatureException, SignatureFormatException {
    Transaction.Contract contract = transaction.getRawData().getContractList().get(0);
    int permissionId = contract.getPermissionId();
    byte[] owner = getOwner(contract);
    AccountCapsule account = accountStore.get(owner);
    Permission permission = null;
    if (account == null) {
      if (permissionId == 0) {
        permission = AccountCapsule.getDefaultPermission(ByteString.copyFrom(owner));
      }
      if (permissionId == 2) {
        permission = AccountCapsule
            .createDefaultActivePermission(ByteString.copyFrom(owner), dynamicPropertiesStore);
      }
    } else {
      permission = account.getPermissionById(permissionId);
    }
    if (permission == null) {
      throw new PermissionException("permission isn't exit");
    }
    checkPermission(permissionId, permission, contract);
    long weight = checkWeight(permission, transaction.getSignatureList(), hash, null);
    if (weight >= permission.getThreshold()) {
      return true;
    }
    return false;
  }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/TransactionCapsule.java (L635-645)
```java
  private static void checkPermission(int permissionId, Permission permission, Transaction.Contract contract) throws PermissionException {
    if (permissionId != 0) {
      if (permission.getType() != PermissionType.Active) {
        throw new PermissionException("Permission type is error");
      }
      //check operations
      if (!checkPermissionOperations(permission, contract)) {
        throw new PermissionException("Permission denied");
      }
    }
  }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/AccountCapsule.java (L1301-1320)
```java
  public void updatePermissions(Permission owner, Permission witness, List<Permission> actives) {
    Builder builder = this.account.toBuilder();

    owner = owner.toBuilder().setId(0).build();
    builder.setOwnerPermission(owner);
    if (witness != null && builder.getIsWitness()) {
      witness = witness.toBuilder().setId(1).build();
      builder.setWitnessPermission(witness);
    }

    builder.clearActivePermission();
    if (actives != null) {
      for (int i = 0; i < actives.size(); i++) {
        Permission permission = actives.get(i).toBuilder().setId(i + 2).build();
        builder.addActivePermission(permission);
      }
    }

    this.account = builder.build();
  }
```
