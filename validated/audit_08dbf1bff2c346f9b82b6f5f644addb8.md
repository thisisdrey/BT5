### Title
Privilege escalation via Active-permission authority over `AccountPermissionUpdateContract` allows an account co-signer to seize Owner control - (File: actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java)

### Summary
`AccountPermissionUpdateActuator` is the actuator that rewrites an account's entire multisig permission structure (Owner, Witness, and up to 8 Active permissions). Enforcement of who may invoke it is delegated entirely to the generic permission/operation-bitmap check used for every contract type, and that check contains no special-case restriction preventing the `AccountPermissionUpdateContract` type itself from being included in a non-Owner ("Active") permission's allowed-operations bitmap. Because the system's global "available contract types" list treats `AccountPermissionUpdateContract` as an ordinary, grantable operation, an Active-tier signer that is (or becomes) authorized for that single bit can unilaterally overwrite the Owner permission and all other permissions of the account — exactly the "normal user promotes themselves to administrator" pattern described in the reference report, just realized through the multisig permission subsystem instead of an HTTP admin-setup endpoint.

### Finding Description
`AccountPermissionUpdateActuator.execute()` unconditionally overwrites the account's permissions with attacker-supplied values and performs no re-check of which permission tier authorized the call: [1](#0-0) 

Authorization for *which* permission may execute a given contract type is delegated to `TransactionCapsule.checkPermission`, which only requires that, for a non-zero (Active) `permissionId`, the permission's `operations` bitmap has the bit set for the contract being executed — there is no exclusion for `AccountPermissionUpdateContract`: [2](#0-1) 

The same unrestricted check is used both when a client signs offline (`checkPermission`) and when the node accepts the transaction (`validateSignature`/`validatePubSignature`): [3](#0-2) 

The actuator's own `validate()`/`checkPermission()` method validates the *contents* of a submitted permission (key count, weights, threshold, operation bits) against `DynamicPropertiesStore.getAvailableContractType()`, but never forbids `AccountPermissionUpdateContract` from being one of the operation bits an Active permission is allowed to carry: [4](#0-3) 

The codebase's own tests confirm this: the *available* contract-type bitmap (what an Active permission is allowed to request) includes `AccountPermissionUpdateContract`, while only the *default* active permission created at account setup happens to exclude it: [5](#0-4) [6](#0-5) 

This mirrors the underlying flaw pattern from the reference report: a state-changing "make me an admin" style action (`AccountPermissionUpdateContract` rewriting Owner permission) is reachable through a lower-trust code path (an Active permission signer) because the framework never special-cases the operation that grants full administrative control over the very permission system that gates it.

### Impact Explanation
If an account's owner configures (or is induced/tricked into configuring, e.g. via a wallet/dApp template, custodial multisig setup, or exchange hot-wallet policy) any Active permission whose `operations` bitmap includes the `AccountPermissionUpdateContract` bit, then any signer who can reach that Active permission's threshold — which is often a *single* lower-trust key intended only for routine operations such as transfers — can submit an `AccountPermissionUpdateContract` transaction with `permission_id` set to that Active permission. The actuator will accept it (per `TransactionCapsule.checkPermission`) and `execute()` will overwrite the Owner permission, Witness permission, and all Active permissions of the account, in place, with values fully controlled by that lower-trust signer. This is a complete account takeover: the attacker can lock out the legitimate owner and other co-signers permanently and gain full unilateral control of funds and contract-execution rights on the account — a concrete unauthorized-account-operation / theft-of-funds outcome.

### Likelihood Explanation
Exploitation requires the account owner to have granted the `AccountPermissionUpdateContract` operation bit to a non-Owner (Active) permission — this is not the default (`createDefaultActivePermission` excludes it) — so it is not exploitable against every account "out of the box." However, nothing in `checkPermission()` or the actuator prevents or even warns against this configuration, and it is explicitly allowed by `DynamicPropertiesStore.getAvailableContractType()`. Multisig setups for exchanges, custodians, and DAO-style shared accounts commonly configure broad Active-permission templates programmatically, making an accidental or malicious inclusion of this bit plausible, and once present, exploitation requires only a single properly formed, signed transaction — no additional preconditions such as compromising the Owner key.

### Recommendation
In `AccountPermissionUpdateActuator.checkPermission()`, explicitly reject any Active (non-Owner) permission whose `operations` bitmap sets the bit corresponding to `ContractType.AccountPermissionUpdateContract`, analogous to how `ClearABIContract` and `UpdateBrokerageContract` are already excluded from `getAvailableContractType()`/`getActiveDefaultOperations()`. Enforce that `AccountPermissionUpdateContract` may only ever be executed under the Owner permission (`permissionId == 0`), by adding a check in `TransactionCapsule.checkPermission` (or in the actuator's `validate()`) that fails validation whenever `contract.getType() == AccountPermissionUpdateContract && contract.getPermissionId() != 0`.

### Proof of Concept
1. Account `A` (Owner key `Ko`) creates an Active permission `P2` (id=2, threshold=1) containing a key `Kx` and an `operations` bitmap in which the bit for `ContractType.AccountPermissionUpdateContract` is set (this is accepted by `checkPermission()` since the type is present in `getAvailableContractType()`), via a normal `AccountPermissionUpdateContract` transaction signed by `Ko` under Owner permission.
2. Holder of `Kx` crafts a new `AccountPermissionUpdateContract` transaction for account `A`, setting `Transaction.Contract.permission_id = 2` (the Active permission id) and a new `Owner` permission whose sole key is `Kx` (or an attacker-controlled address), and signs it only with `Kx`.
3. Node-side validation (`TransactionCapsule.validateSignature` → `checkPermission`) accepts the transaction because permission `P2` is type `Active` and its `operations` bit for `AccountPermissionUpdateContract` is set, per [2](#0-1) .
4. `AccountPermissionUpdateActuator.execute()` calls `account.updatePermissions(...)`, replacing the Owner permission with the attacker's key, per [1](#0-0) .
5. `Ko` (the original owner) no longer controls account `A`; `Kx`'s holder now has full Owner-level control.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L47-52)
```java
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
