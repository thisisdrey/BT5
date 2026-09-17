## Finding

The FT4 bug involves an actuator (`update_main_auth_descriptor`) that changes an account's most-privileged authority record without validating that the resulting descriptor retains the required capability flags, allowing that authority to be gutted so funds become inaccessible. The closest reachable analog in java-tron is `AccountPermissionUpdateActuator`, which rewrites an account's `Owner`/`Witness`/`Active` permission set (TRON's equivalent of "auth descriptors" with an "operations" capability bitmap analogous to the `A`/`T` flags).

### Title
Active permission holders can be granted (by default) the ability to overwrite the Owner permission via `AccountPermissionUpdateContract`, enabling account takeover — (File: `actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java`)

### Summary
`AccountPermissionUpdateActuator.checkPermission()` validates an `Active` permission's `operations` bitmap only against the globally available contract types (`dynamicStore.getAvailableContractType()`), never excluding `AccountPermissionUpdateContract` itself from what an `Active` (non-Owner) permission may authorize. [1](#0-0) 

### Finding Description
`validate()` requires an `Owner` permission and 1–8 `Active` permissions but performs no check restricting which `permissionId` may authorize an `AccountPermissionUpdateContract` transaction; it simply calls `checkPermission()` on each supplied permission and writes them via `account.updatePermissions(...)` in `execute()`. [2](#0-1) [3](#0-2) 

Whether a given signature (and its `permissionId`) is authorized to submit a specific contract type is decided independently in `TransactionCapsule.checkPermission()`: if `permissionId != 0` (i.e., the signer is using an `Active` permission, not `Owner`), the code only checks that the `Active` permission's `type` is `Active` and that its `operations` bitmap contains a bit for the current contract type — it does not special-case or forbid `AccountPermissionUpdateContract`. [4](#0-3) 

The project's own test suite documents that the default `Active` permission's operation bitmap covers essentially every contract type except `ClearABIContract` and `UpdateBrokerageContract`: [5](#0-4) 

Consequently, if an account owner grants any co-signer a default (or custom, unrestricted) `Active` permission — a routine multisig setup action — that co-signer's key alone can submit a new `AccountPermissionUpdateContract` using their `Active` `permissionId`, and the actuator will accept it and overwrite the account's `Owner`, `Witness`, and `Active` permission set with attacker-controlled keys, with no check that the acting signer holds the `Owner` permission and no re-validation that the replaced `Owner`/`Active` set is one the original owner still controls. This exactly mirrors the FT4 root cause — a privileged-authority-mutating operation with insufficient validation of the resulting authority's required capabilities/ownership — except here it is worse because it also fails to gate execution to the most-privileged tier at all.

### Impact Explanation
An account holder who grants a co-signer an `Active` permission (a normal, encouraged multisig pattern) unknowingly exposes the account to a full takeover: that co-signer can unilaterally replace the `Owner` permission (and remove the original owner's keys entirely), then use full `Owner` rights to drain or permanently control all TRX/TRC10/TRC20 held by the account. This is concrete unauthorized account takeover and potential permanent freezing/theft of funds.

### Likelihood Explanation
The precondition — granting any `Active` permission with unrestricted (or default) `operations` — is the standard, documented way to add co-signers in TRON's multisig model, and `AccountPermissionUpdateContract` is not excluded from the default operations bitmap. No special privilege beyond holding a granted `Active` key is required; this is reachable via a single signed transaction from any account that has previously been given an `Active` permission by its owner.

### Recommendation
In `AccountPermissionUpdateActuator.validate()` (and/or in `TransactionCapsule.checkPermission()`), require that `AccountPermissionUpdateContract` may only be authorized by `permissionId == 0` (the `Owner` permission), regardless of what bits are set in an `Active` permission's `operations` bitmap. Additionally, consider excluding `AccountPermissionUpdateContract` from the set of contract types eligible to be enabled in any `Active` permission's operations bitmap in `checkPermission()`, mirroring how `ClearABIContract`/`UpdateBrokerageContract` are already excluded.

### Proof of Concept
1. Account `A` (owner) calls `AccountPermissionUpdateContract` to add co-signer `B`'s address into an `Active` permission with default `operations` (which includes the bit for `AccountPermissionUpdateContract`), per `AccountPermissionUpdateActuator.execute()`. [6](#0-5) 
2. `B` signs a new `AccountPermissionUpdateContract` transaction with `permissionId` set to the `Active` permission's id, replacing `Owner` with `B`'s own key and removing `A`'s key entirely.
3. Because `TransactionCapsule.checkPermission()` only verifies that the `Active` permission's `operations` bit for `AccountPermissionUpdateContract` is set — which it is by default — the transaction validates and is executed, permanently locking `A` out of the account and giving `B` full `Owner` control. [4](#0-3) 

**Note on confidence:** I could not directly confirm from the indexed files the exact numeric position of `AccountPermissionUpdateContract` within the `ContractType` enum or fully render the default `operations` bitmap byte-by-byte (the test only shows the aggregate hex value and lists two excluded types by name). This detail would need to be verified in the full `Protocol.proto`/`AccountCapsule` source, which may not be fully covered by the current index — a Devin session with full repo access could confirm this precisely.

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

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L124-145)
```java
    ByteString operations = permission.getOperations();
    if (permission.getType() != PermissionType.Active) {
      if (!operations.isEmpty()) {
        throw new ContractValidateException(
            permission.getType() + " permission needn't operations");
      }
      return true;
    }
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

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L148-228)
```java
  @Override
  public boolean validate() throws ContractValidateException {

    if (chainBaseManager == null) {
      throw new ContractValidateException(ActuatorConstant.STORE_NOT_EXIST);
    }

    if (this.any == null) {
      throw new ContractValidateException(ActuatorConstant.CONTRACT_NOT_EXIST);
    }

    AccountStore accountStore = chainBaseManager.getAccountStore();
    DynamicPropertiesStore dynamicStore = chainBaseManager.getDynamicPropertiesStore();

    if (dynamicStore.getAllowMultiSign() != 1) {
      throw new ContractValidateException("multi sign is not allowed, "
          + "need to be opened by the committee");
    }
    if (!this.any.is(AccountPermissionUpdateContract.class)) {
      throw new ContractValidateException(
          "contract type error,expected type [AccountPermissionUpdateContract],real type["
              + any.getClass() + "]");
    }
    final AccountPermissionUpdateContract accountPermissionUpdateContract;
    try {
      accountPermissionUpdateContract = any.unpack(AccountPermissionUpdateContract.class);
    } catch (InvalidProtocolBufferException e) {
      logger.debug(e.getMessage(), e);
      throw new ContractValidateException(e.getMessage());
    }
    byte[] ownerAddress = accountPermissionUpdateContract.getOwnerAddress().toByteArray();
    if (!DecodeUtil.addressValid(ownerAddress)) {
      throw new ContractValidateException("invalidate ownerAddress");
    }
    AccountCapsule accountCapsule = accountStore.get(ownerAddress);
    if (accountCapsule == null) {
      throw new ContractValidateException("ownerAddress account does not exist");
    }

    if (!accountPermissionUpdateContract.hasOwner()) {
      throw new ContractValidateException("owner permission is missed");
    }

    if (accountCapsule.getIsWitness()) {
      if (!accountPermissionUpdateContract.hasWitness()) {
        throw new ContractValidateException("witness permission is missed");
      }
    } else {
      if (accountPermissionUpdateContract.hasWitness()) {
        throw new ContractValidateException("account isn't witness can't set witness permission");
      }
    }

    if (accountPermissionUpdateContract.getActivesCount() == 0) {
      throw new ContractValidateException("active permission is missed");
    }
    if (accountPermissionUpdateContract.getActivesCount() > 8) {
      throw new ContractValidateException("active permission is too many");
    }

    Permission owner = accountPermissionUpdateContract.getOwner();
    Permission witness = accountPermissionUpdateContract.getWitness();
    List<Permission> actives = accountPermissionUpdateContract.getActivesList();

    if (owner.getType() != PermissionType.Owner) {
      throw new ContractValidateException("owner permission type is error");
    }
    checkPermission(owner);
    if (accountCapsule.getIsWitness()) {
      if (witness.getType() != PermissionType.Witness) {
        throw new ContractValidateException("witness permission type is error");
      }
      checkPermission(witness);
    }
    for (Permission permission : actives) {
      if (permission.getType() != PermissionType.Active) {
        throw new ContractValidateException("active permission type is error");
      }
      checkPermission(permission);
    }
    return true;
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
