### Title
Active permission with `AccountPermissionUpdateContract` enabled can overwrite the Owner permission, allowing privilege escalation - (`File: actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java`)

### Summary
`AccountPermissionUpdateActuator` allows any signer whose `Permission` — including a low-privilege `Active` permission — authorizes the `AccountPermissionUpdateContract` type to overwrite the account's `Owner` permission unconditionally, without any check that the signing permission is actually the `Owner` permission (id 0). This mirrors the reported BENQI `StakingContract` bug class where a role intended to be subordinate (BENQI super-admin / `DEFAULT_ADMIN_ROLE`) could control/overwrite roles above or beside it because the contract never verified that the specific privileged role — not merely "any authorized signer" — performed the sensitive action.

### Finding Description
When java-tron validates whether a transaction's signature suffices for a given contract, it resolves the `Permission` object to use purely from the `permission_id` field embedded in the transaction's `Contract` message, then checks whether that permission's `operations` bitmap allows the contract type being executed: [1](#0-0) 

For non-owner permissions (`permissionId != 0`), the check performed by `Wallet.getTransactionApprovedList` (and equivalently during actual broadcast/execution) is only that the permission type is `Active` and that its `operations` bitmap has the bit set for the contract's type: [2](#0-1) 

Crucially, `AccountPermissionUpdateContract` (contract type 46) is a valid, generally-available contract type that can be included in an `Active` permission's `operations` bitmap — the actuator's own test explicitly demonstrates that the full valid-operations bitmask includes essentially every contract type except `ClearABIContract` and `UpdateBrokerageContract`: [3](#0-2) 

`AccountPermissionUpdateActuator.validate()`/`checkPermission()` only checks the *content* of the new permissions being proposed (key counts, thresholds, parent id, allowed operation bits) — it never checks which permission ID was used to authorize/sign the transaction itself: [4](#0-3) 

And `execute()` unconditionally overwrites the account's `owner`, `witness`, and `active` permissions with whatever was supplied in the contract, via `AccountCapsule.updatePermissions`: [5](#0-4) [6](#0-5) 

So, if an account's `Owner` sets up an `Active` permission (e.g., for a delegated operator, custodial signer, or automated service) whose `operations` bitmap happens to include bit 46 (`AccountPermissionUpdateContract`) — which is a normal, permitted bit per the default available-contract-type mask — that Active-permission signer can broadcast an `AccountPermissionUpdateContract` transaction with `permission_id = <active id>` and freely rewrite the `Owner` permission (and all `Active`/`Witness` permissions) to anything they want, including removing the legitimate owner's keys entirely. This is structurally the same root cause as the external report: a role/permission tier that is supposed to be subordinate is not prevented from managing the permission tier above it, because the sensitive "manage roles/permissions" action is authorized generically (any signer whose bitmap allows the contract type) instead of being pinned to the specific top-level role (`Owner`, id 0).

### Impact Explanation
An attacker who controls (or is granted) only an `Active` permission with `AccountPermissionUpdateContract` enabled in its operation bitmap can hijack the entire account by overwriting the `Owner` permission, permanently locking out the legitimate owner and other permission holders and gaining exclusive unauthorized control of the account and its funds. This satisfies "concrete unauthorized account operation / permanent freezing of funds" impact criteria, since account owners commonly grant delegated `Active` permissions for automation, and there is no protocol-level restriction preventing `AccountPermissionUpdateContract` from being included in such bitmaps.

### Likelihood Explanation
Exploitability requires that an account's owner configure an `Active` permission whose `operations` bitmap includes bit 46. This is not blocked by any validation — `checkPermission()` in `AccountPermissionUpdateActuator` only rejects bits outside `dynamicStore.getAvailableContractType()`, which includes `AccountPermissionUpdateContract`. Wallets/tooling that build "full access" or broadly-scoped active permissions (or that copy the default operations mask without deliberately excluding contract 46) would unintentionally create this exposure, and a malicious or compromised holder of such an Active key could exploit it directly with a single signed transaction — no special network position or additional privilege is required beyond holding that Active key.

### Recommendation
Enforce that `AccountPermissionUpdateContract` can only be authorized via the account's `Owner` permission (`permission_id == 0`), regardless of what any `Active` permission's `operations` bitmap allows — e.g., by special-casing this contract type in `Wallet.getTransactionApprovedList`/signature validation and in `AccountPermissionUpdateActuator.validate()`, or by excluding contract type 46 from the set of bits assignable to non-owner permissions (similar to how `ClearABIContract` and `UpdateBrokerageContract` are already excluded from `checkAvailableContractTypeCorrespondingToCode`).

### Proof of Concept
1. Account `A` has default `Owner` permission (id 0) and an `Active` permission (id 2) whose `operations` bitmap has bit 46 set (`AccountPermissionUpdateContract` allowed), granted to key `K_active` for automation purposes — this configuration passes `AccountPermissionUpdateActuator.checkPermission()` since bit 46 is within `getAvailableContractType()`.
2. Attacker who controls `K_active` crafts an `AccountPermissionUpdateContract` transaction for account `A`, sets `owner_address = A`, `owner = <attacker-controlled Owner permission with only attacker's key>`, and any `actives`, sets `Contract.Permission_id = 2` (the active permission id), and signs with `K_active`.
3. `Wallet.getTransactionApprovedList`/validateSignature accepts the signature because `permission.getType() == Active` and `WalletUtil.checkPermissionOperations` returns true (bit 46 is set) — see `framework/src/main/java/org/tron/core/Wallet.java` lines 665-678.
4. `AccountPermissionUpdateActuator.execute()` calls `account.updatePermissions(...)`, unconditionally overwriting the `Owner` permission with the attacker's key — `chainbase/src/main/java/org/tron/core/capsule/AccountCapsule.java` lines 1301-1319.
5. Account `A`'s legitimate owner is now permanently locked out; the attacker has full control.

### Citations

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

**File:** framework/src/main/java/org/tron/core/Wallet.java (L659-678)
```java
        Contract contract = trx.getRawData().getContract(0);
        byte[] owner = TransactionCapsule.getOwner(contract);
        AccountCapsule account = chainBaseManager.getAccountStore().get(owner);
        if (account == null) {
          throw new PermissionException("Account does not exist!");
        }
        int permissionId = contract.getPermissionId();
        Permission permission = account.getPermissionById(permissionId);
        if (permission == null) {
          throw new PermissionException("Permission for this, does not exist!");
        }
        if (permissionId != 0) {
          if (permission.getType() != PermissionType.Active) {
            throw new PermissionException("Permission type is wrong!");
          }
          //check operations
          if (!WalletUtil.checkPermissionOperations(permission, contract)) {
            throw new PermissionException("Permission denied!");
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

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L34-52)
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
```

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L148-221)
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
```

**File:** chainbase/src/main/java/org/tron/core/capsule/AccountCapsule.java (L1301-1319)
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
```
