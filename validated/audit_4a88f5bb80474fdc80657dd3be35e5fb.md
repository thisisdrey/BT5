Confirmed: `execute()` in `AccountPermissionUpdateActuator` calls `account.updatePermissions(...)` unconditionally, overwriting owner, witness, and all active permissions with whatever the transaction contains, without checking which permission (`permission_id`) actually signed the transaction. [1](#0-0) 

### Title
Active-key privilege escalation to full account takeover via unrestricted AccountPermissionUpdateContract execution - (File: actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java)

### Summary
The `addBSPOperator` bug class describes a role-assignment function that only checks a coarse-grained permission (`onlyGovernor`) instead of validating that the caller has authority specific to the role being assigned. The java-tron analog is `AccountPermissionUpdateContract`/`AccountPermissionUpdateActuator`: the multisig "active" permission mechanism only checks a per-operation bitmask (whether the operation `ContractType` bit is set) before allowing an active key to sign a given contract type, but this same coarse check is applied even when the contract being authorized is `AccountPermissionUpdateContract` itself, which lets that active key rewrite the account's Owner, Witness, and all Active permissions arbitrarily.

### Finding Description
When a transaction is signed by a non-owner (`permission_id != 0`) permission, `TransactionCapsule.checkPermission`/`checkPermissionOperations` only verifies that the specific `Permission`'s `operations` bitmap has the bit set for the contract's `ContractType` value; it does not restrict which contract types may be delegated to an Active permission. [2](#0-1) [3](#0-2) 

`AccountPermissionUpdateContract` (`ContractType` id 46) is a normal, assignable operation bit within the default `availableContractType` bitmap, as demonstrated by the actuator's own test enumerating all contract types into the operations bitmap. [4](#0-3) 

`AccountPermissionUpdateActuator.validate()` performs structural checks on the submitted `owner`, `witness`, and `active` permission lists (key counts, thresholds, names, operations bitmap validity) but never checks or restricts based on which permission signed the transaction (i.e., it never inspects `contract.getPermissionId()` to require that only the Owner permission may invoke this contract type). [5](#0-4) 

`execute()` then unconditionally overwrites the account's owner permission, witness permission, and all active permissions with attacker-supplied `Permission` objects. [1](#0-0) 

Consequently, if an account owner ever grants an Active permission a broad operation set that includes bit 46 (`AccountPermissionUpdateContract`) — which is easy to do inadvertently since the actuator's own validation explicitly allows any bit within `availableContractType` and provides no warning or restriction against including bit 46 — the holder of that Active key can sign an `AccountPermissionUpdateContract` transaction (with `permission_id` set to their own active permission id) and replace the Owner permission's keys entirely, replace all Active permissions, and (if the account is a witness) replace the Witness permission. This is functionally identical to the reported bug class: a role-management function ("assign role") is gated only by a coarse, mis-scoped check (an operation-bitmap bit that was never meant to authorize root-level role reassignment) instead of a role-specific authorization (requiring the true Owner permission, id 0, to sign).

### Impact Explanation
An account whose owner delegated an Active key with the `AccountPermissionUpdateContract` bit set (whether intentionally for convenience or via a poorly-scoped custodial/smart-wallet setup) is fully takeable over by that Active key holder: the attacker can install their own address as the sole Owner key, evict the legitimate owner's keys from Owner/Active/Witness permissions, and permanently lock the true owner out — a concrete unauthorized account takeover with permanent loss of control over funds and, for witness accounts, block-production authority.

### Likelihood Explanation
This requires a pre-existing condition — an Active permission whose `operations` bitmap includes bit 46 — but nothing in `checkPermission` in `AccountPermissionUpdateActuator.java` prevents or warns against including that bit, and the multisig feature is explicitly designed to let users construct arbitrary operation bitmaps for active keys. Any wallet, exchange integration, or custodial signer that grants a broad or "all contract types" active permission (a common simplification) is exposed. The exploit itself is a single signed transaction from the already-authorized Active key, requiring no additional privilege escalation.

### Recommendation
Require that `AccountPermissionUpdateContract` (and similarly sensitive contract types affecting root account control) can only be authorized by the Owner permission (`permission_id == 0`), regardless of what bits are set in an Active permission's operations bitmap. This should be enforced in `AccountPermissionUpdateActuator.validate()` by rejecting the transaction if `contract.getPermissionId() != 0`, and/or by excluding bit 46 from the set of contract types that can legally appear in an Active permission's `operations` bitmap during `checkPermission` in the actuator.

### Proof of Concept
1. Owner account `A` creates an Active permission `P` (id 2) with an `operations` bitmap that includes bit 46 (`AccountPermissionUpdateContract`), e.g. by granting a "full access" active key to a delegate for convenience, and assigns key `K` to `P`.
2. Delegate holding `K` crafts a transaction with `Contract.type = AccountPermissionUpdateContract`, `Contract.Permission_id = 2` (referring to `P`), setting a brand-new Owner permission containing only the delegate's own address/key, and arbitrary Active/Witness permissions.
3. Delegate signs with `K`. `TransactionCapsule.checkPermission`/`checkPermissionOperations` passes because bit 46 is set in `P.operations`. [2](#0-1) 
4. `AccountPermissionUpdateActuator.validate()` passes (it only checks structural validity of the new permissions, not who signed). [5](#0-4) 
5. `execute()` overwrites account `A`'s Owner/Witness/Active permissions with the delegate's chosen values, giving the delegate sole control of `A`. [1](#0-0)

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L43-52)
```java
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

**File:** chainbase/src/main/java/org/tron/core/capsule/TransactionCapsule.java (L635-644)
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
```

**File:** actuator/src/main/java/org/tron/core/utils/TransactionUtil.java (L171-180)
```java
  public static boolean checkPermissionOperations(Permission permission, Contract contract)
      throws PermissionException {
    ByteString operations = permission.getOperations();
    if (operations.size() != 32) {
      throw new PermissionException("operations size must be 32");
    }
    int contractType = contract.getTypeValue();
    boolean b = (operations.byteAt(contractType / 8) & (1 << (contractType % 8))) != 0;
    return b;
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
