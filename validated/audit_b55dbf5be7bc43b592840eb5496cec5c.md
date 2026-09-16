## Analog Found

### Title
Scoped Active permission with `AccountPermissionUpdateContract` authorization can overwrite the account's Owner permission and seize full control - (File: `actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java`)

### Summary
`AccountPermissionUpdateActuator` allows any signer whose Active permission's operation bitmap authorizes `AccountPermissionUpdateContract` to completely replace an account's Owner permission, Witness permission, and all Active permissions in a single call, with no check that the transaction was actually signed by the Owner permission itself. This mirrors the Nextcloud bug class where a scoped credential (an apptoken restricted from filesystem access) could still modify the master permission/token structure that should be beyond its scope — java-tron's scoped "Active" multisig key, if ever granted the `AccountPermissionUpdateContract` bit, is not limited to managing only its own sub-permission and can instead hijack the Owner permission wholesale.

### Finding Description
Transaction-level permission enforcement for any contract is done in `TransactionCapsule.validateSignature` / `checkPermission`, which resolves the signer's permission via `account.getPermissionById(permissionId)` and then calls `checkPermissionOperations`: [1](#0-0) [2](#0-1) 

`checkPermissionOperations` only checks whether the contract-type bit is set in the signer's Active permission `operations` bitmap — it makes no exception for `AccountPermissionUpdateContract` and imposes no requirement that permission-management operations must come from the Owner permission (`permissionId == 0`).

Whether an Active permission is allowed to carry the `AccountPermissionUpdateContract` bit is controlled purely by `DynamicPropertiesStore.getAvailableContractType()`, which — unlike `getActiveDefaultOperations()` (the *default* bitmap for freshly created active permissions, which does exclude `AccountPermissionUpdateContract`) — includes `AccountPermissionUpdateContract` as a valid bit an owner can explicitly grant to a scoped Active key: [3](#0-2) [4](#0-3) 

Once a transaction signed by such a scoped Active key passes that single bitmap check, `AccountPermissionUpdateActuator.validate()`/`execute()` performs **no further authorization distinction based on which permission signed the transaction**. It only validates the *shape* of the new permissions (key counts, thresholds, name length, operations bitmap subset of `getAvailableContractType()`), and then unconditionally overwrites the account's Owner, Witness, and Active permissions: [5](#0-4) [6](#0-5) 

There is no code path in `validate()` or `execute()` that checks `contract.getPermissionId() == 0` (i.e., that the update was authorized by the Owner permission) before allowing the Owner permission field to be replaced. This is precisely the missing check the Nextcloud report calls out: "Only allow tokens that result from a real login to modify/delete tokens" / "Do not allow the current token in use to edit itself" — here, a scoped credential (Active permission) that has been authorized only for a narrow operation set can, via that same authorization channel, redefine the master credential (Owner permission) of the account.

### Impact Explanation
If an account owner ever grants an Active (multisig sub-key) permission the `AccountPermissionUpdateContract` bit — a legitimate and expected configuration for delegated permission management — the holder(s) of that Active key can unilaterally replace the account's Owner permission with keys of their own choosing (e.g., threshold 1, single attacker-controlled key), permanently and completely seizing account control, including all TRX/TRC10/TRC20 balances and any resources delegated to the account. This is a full, irreversible account takeover achievable by a party that was only ever meant to hold a scoped/limited signing capability, matching the "unauthorized account operation / theft or permanent freezing of funds" bar.

### Likelihood Explanation
Exploitation requires that the account owner has configured an Active permission whose `operations` bitmap includes the `AccountPermissionUpdateContract` bit — which is explicitly supported and validated as legal by `checkPermission`/`getAvailableContractType()` (unlike the safer default bitmap `getActiveDefaultOperations()`, which excludes it). Any multisig setup intended to delegate "permission management" duties to a co-signer/scoped key (a plausible and likely real-world configuration, e.g. an exchange or custodian delegating key-rotation duties to an operational key) is directly exposed. No special network conditions, races, or additional exploits are needed — a single signed `AccountPermissionUpdateContract` transaction from the scoped key suffices.

### Recommendation
In `AccountPermissionUpdateActuator.validate()`, require that a transaction modifying the Owner permission (or Witness permission) must be signed via the Owner permission itself (`contract.getPermissionId() == 0`), regardless of whether the signer's Active permission's operations bitmap happens to include `AccountPermissionUpdateContract`. Alternatively/additionally, disallow `AccountPermissionUpdateContract` from ever being included in `getAvailableContractType()`'s legal bit set for Active permissions, since it does not need to be independently grantable to sub-keys distinct from the Owner permission's inherent authority.

### Proof of Concept
1. Account `A` sets up multisig via `AccountPermissionUpdateContract`: Owner permission = key `O`; Active permission (id=2) = key `S` (a "scoped" delegated key), with `operations` bitmap explicitly including the `AccountPermissionUpdateContract` bit (a valid configuration per `checkPermission`/`getAvailableContractType`, see `AccountPermissionUpdateActuatorTest.checkAvailableContractType`).
2. Attacker who controls key `S` (but not `O`) crafts a new `AccountPermissionUpdateContract` transaction with `permissionId = 2`, setting: `owner` = new Owner permission containing only attacker's key `X` with threshold 1; `actives` = anything satisfying `checkPermission`.
3. Attacker signs the transaction with key `S` only.
4. `TransactionCapsule.validateSignature` → `checkPermission(2, activePermission_S, contract)` → `WalletUtil.checkPermissionOperations` returns true because the `AccountPermissionUpdateContract` bit is set in `S`'s operations — signature validation passes (`chainbase/src/main/java/org/tron/core/capsule/TransactionCapsule.java:468-491,635-645`).
5. `AccountPermissionUpdateActuator.validate()`/`execute()` accept and apply the update, overwriting account `A`'s Owner permission to attacker's key `X` (`actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java:44-52,148-229`).
6. Attacker now fully owns account `A` via key `X`, with the original owner key `O` and delegated key `S` both potentially removed from the new permission set.

### Citations

**File:** chainbase/src/main/java/org/tron/core/capsule/TransactionCapsule.java (L468-491)
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
```

**File:** chainbase/src/main/java/org/tron/common/utils/WalletUtil.java (L27-37)
```java
  public static boolean checkPermissionOperations(Permission permission, Contract contract)
      throws PermissionException {
    ByteString operations = permission.getOperations();
    if (operations.size() != 32) {
      throw new PermissionException(String.format("operations size must 32, actual: %d",
          operations.size()));
    }
    int contractType = contract.getTypeValue();
    boolean b = (operations.byteAt(contractType / 8) & (1 << (contractType % 8))) != 0;
    return b;
  }
```

**File:** framework/src/test/java/org/tron/core/actuator/AccountPermissionUpdateActuatorTest.java (L979-997)
```java
  @Test
  public void checkAvailableContractType() {
    String validContractType = "7fff1fc0037ef90f000000000000000000000000000000000000000000000000";

    byte[] availableContractType = new byte[32];
    for (ContractType contractType : ContractType.values()) {
      if (contractType == org.tron.protos.Protocol.Transaction.Contract.ContractType.UNRECOGNIZED
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

**File:** framework/src/test/java/org/tron/core/actuator/AccountPermissionUpdateActuatorTest.java (L1000-1020)
```java
  @Test
  public void checkActiveDefaultOperations() {
    String validContractType = "7fff1fc0033ef90f000000000000000000000000000000000000000000000000";

    byte[] availableContractType = new byte[32];
    for (ContractType contractType : ContractType.values()) {
      if (contractType == org.tron.protos.Protocol.Transaction.Contract.ContractType.UNRECOGNIZED
          || contractType == ContractType.AccountPermissionUpdateContract
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

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L44-52)
```java
    try {
      accountPermissionUpdateContract = any.unpack(AccountPermissionUpdateContract.class);

      byte[] ownerAddress = accountPermissionUpdateContract.getOwnerAddress().toByteArray();
      AccountCapsule account = accountStore.get(ownerAddress);
      account.updatePermissions(accountPermissionUpdateContract.getOwner(),
          accountPermissionUpdateContract.getWitness(),
          accountPermissionUpdateContract.getActivesList());
      accountStore.put(ownerAddress, account);
```

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L148-229)
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
  }
```
