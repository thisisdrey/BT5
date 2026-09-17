## Analysis

The OpenClaw bug class is: a scoped, lesser-privileged capability surface (`operator.write` via `browser.request`) is supposed to be denied from reaching a specific destructive management action, and the deny-list that implements this restriction (`isPersistentBrowserProfileMutation`) explicitly blocks two sibling routes (`POST /profiles/create`, `DELETE /profiles/:name`) but omits a third, equally destructive sibling route (`POST /reset-profile`), letting the same privilege tier reach total control (stop/close/wipe) it was never meant to hold.

java-tron has the exact same shape of gate: the "Active" permission tier (the multisig analog of a scoped, lesser-privileged key/role, comparable to `operator.write`) can only execute the `ContractType`s whose bit is set in its `operations` bitmap, and that bitmap is itself constrained by `availableContractType`, which is the authoritative deny-list of contract types considered too sensitive to ever grant to a non-Owner permission. [1](#0-0) 

That test enumerates the deny-list explicitly: `ContractType.UNRECOGNIZED`, `ContractType.ClearABIContract`, and `ContractType.UpdateBrokerageContract` are the only types excluded from `availableContractType` — i.e., the only contract types an owner can never delegate to an Active permission. `ContractType.AccountPermissionUpdateContract` — the contract type that rewrites the account's entire Owner/Witness/Active permission structure — is conspicuously **not** on that exclusion list, so it is a legally grantable bit for a low-threshold Active permission. [2](#0-1) 

`checkPermission` only validates the shape of the *new* permission being installed (key count, weight sum, threshold, and that requested operation bits are inside `availableContractType`) — it never inspects which permission tier *signed* the incoming transaction relative to what is being rewritten. [3](#0-2) 

`validate()` likewise never checks `contract.getPermissionId()` against `PermissionType.Owner`; it only checks structural validity of the owner/witness/actives payload. [4](#0-3) 

`execute()` unconditionally calls `account.updatePermissions(...)` with whatever Owner/Witness/Actives the transaction supplies, again without checking the signer's permission tier.

The gate that decides "is this signer even allowed to submit this ContractType" lives entirely upstream in `TransactionCapsule`/`WalletUtil`: [5](#0-4) 

`checkPermissionOperations` just tests whether `contract.getTypeValue()`'s bit is set in the signer's `permission.getOperations()` bitmap — it has no special-casing that forbids `AccountPermissionUpdateContract` from being reachable via a non-Owner (`permissionId != 0`) signature path, the same way `ClearABIContract`/`UpdateBrokerageContract` are structurally prevented from ever being grantable. [6](#0-5) 

This is the exact residual-gap pattern from the report: two sibling "sensitive contract types" (`ClearABIContract`, `UpdateBrokerageContract`) are correctly hard-excluded from ever being delegable to the lesser-privileged tier, but the most sensitive sibling of all — the one that can rewrite the account's entire authority graph — was left off that same exclusion list.

### Title
Active (non-Owner) multisig permission can be granted `AccountPermissionUpdateContract`, allowing self-escalation to full account (Owner-level) control - (File: `actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java`)

### Summary
`availableContractType`, the deny-list that governs which `ContractType`s can ever be delegated to a non-Owner ("Active") multisig permission, excludes only `ClearABIContract` and `UpdateBrokerageContract` as too sensitive for delegation. `AccountPermissionUpdateContract` — the contract type that overwrites the account's Owner, Witness, and Active permission structures — is not excluded, and neither `AccountPermissionUpdateActuator.validate()`/`execute()` nor `TransactionCapsule.checkPermission`/`WalletUtil.checkPermissionOperations` verify that the signer used a `permissionId` corresponding to the Owner permission before allowing a full permission rewrite.

### Finding Description
Tron's multisig model intends a strict privilege hierarchy: Owner permission is authoritative and controls all account state including sub-permissions; Active permissions are scoped, lower-privilege keys restricted to the `ContractType`s explicitly enabled in their 32-byte `operations` bitmap, and that bitmap itself can only contain bits present in the node-wide `availableContractType` allow-set ( [7](#0-6) ). The codebase already recognizes some contract types are too dangerous to ever delegate — `ClearABIContract` and `UpdateBrokerageContract` are hard-excluded from `availableContractType` in every place the bitmap is constructed/tested ( [8](#0-7) ). `AccountPermissionUpdateContract` is not in that exclusion set, so an Owner who (directly, via tooling default, or via social engineering of an operator workflow) enables this bit for an Active permission with a low threshold creates a key that can call `AccountPermissionUpdateContract` itself. Once that transaction is submitted and signed with only the Active permission's (lower) threshold, `AccountPermissionUpdateActuator.execute()` calls `account.updatePermissions(...)` unconditionally, replacing Owner/Witness/Active permissions with attacker-chosen keys and thresholds ( [9](#0-8) ). Nothing in `validate()`/`execute()`, nor in the transaction-level checks `TransactionCapsule.checkPermission`/`WalletUtil.checkPermissionOperations`, restricts `AccountPermissionUpdateContract` to signers holding the actual Owner permission (`permissionId == 0`) — the check is purely "is this ContractType's bit set in whatever permission signed it" ( [6](#0-5) , [5](#0-4) ).

### Impact Explanation
A low-threshold, intentionally scoped Active key that is (or becomes) enabled for `AccountPermissionUpdateContract` can unilaterally rewrite the account's Owner permission — inserting attacker-controlled keys with full weight/threshold — achieving complete, permanent account takeover, including subsequent free rein to transfer all funds, freeze/unfreeze balances, and grant itself any other capability (including ones normally reserved for Owner). This is concrete unauthorized account takeover, matching the "concrete unauthorized account operation" bar.

### Likelihood Explanation
Exploitation requires only that the specific Active permission's `operations` bitmap include the `AccountPermissionUpdateContract` bit at the time of the attack — a state reachable through misconfiguration, third-party wallet/tooling defaults that set broad operation bitmaps, or a previous legitimate but overly permissive grant. Given `AccountPermissionUpdateContract` is not blocked at the allow-set level the way `ClearABIContract`/`UpdateBrokerageContract` are, there is no defense-in-depth once such a grant exists; a single signed transaction meeting only the Active threshold suffices.

### Recommendation
Add `ContractType.AccountPermissionUpdateContract` to the exclusion list enforced in `availableContractType` construction (alongside `ClearABIContract`, `UpdateBrokerageContract`) so it can never be enabled in any Active permission's operations bitmap, and/or add an explicit check in `AccountPermissionUpdateActuator.validate()`/`TransactionCapsule.checkPermission` that rejects any `AccountPermissionUpdateContract` transaction unless it is signed under `permissionId == 0` (Owner).

### Proof of Concept
1. Owner account has an Active permission (id 2, threshold 1) whose `operations` bitmap includes bit 46 (`AccountPermissionUpdateContract`) — either via direct owner action or third-party tooling default that enables broadly, since this bit is not blocked by `availableContractType`.
2. Attacker holding the single Active-permission private key crafts an `AccountPermissionUpdateContract` transaction setting `owner` permission to a key they control with full weight/threshold, sets `permission_id = 2` on the contract.
3. Sign with the Active key; submit via `broadcastTransaction`.
4. `TransactionCapsule.validateSignature` finds `permission = account.getPermissionById(2)`, calls `checkPermission(2, permission, contract)` → `checkPermissionOperations` returns true because bit 46 is set; weight (1) meets threshold (1) → signature accepted ( [10](#0-9) ).
5. `AccountPermissionUpdateActuator.validate()`/`execute()` runs with no check on `permission_id`, and `account.updatePermissions(...)` installs the attacker's Owner permission, completing account takeover.

### Citations

**File:** framework/src/test/java/org/tron/core/actuator/AccountPermissionUpdateActuatorTest.java (L919-948)
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

**File:** framework/src/test/java/org/tron/core/actuator/AccountPermissionUpdateActuatorTest.java (L979-998)
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

  }
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
