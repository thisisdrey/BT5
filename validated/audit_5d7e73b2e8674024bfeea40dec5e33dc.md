### Title
Active-permission signer can overwrite Owner/Witness permissions via `AccountPermissionUpdateContract`, escalating to full account takeover - ([File: actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java])

### Summary
`AccountPermissionUpdateActuator.execute()` unconditionally applies whatever `owner`, `witness`, and `actives` permissions are contained in an `AccountPermissionUpdateContract`, without verifying that the permission tier used to authorize the transaction (`Contract.getPermissionId()`) is actually entitled to rewrite a *higher-privileged* permission (Owner/Witness). The only gate that exists is a capability-bit check (`checkPermissionOperations`) that merely confirms the signer's Active permission is allowed to send this *contract type* at all — it never checks which *fields* of the account permission structure the signer is allowed to modify.

### Finding Description
When a transaction of type `AccountPermissionUpdateContract` is broadcast, signature/permission validation is done generically by `TransactionCapsule.validateSignature()`: [1](#0-0) 

For a non-zero `permissionId` (i.e., an Active permission, not Owner), the only requirement is `permission.getType() == Active` and that the permission's `operations` bitmap has the bit for the contract's type set: [2](#0-1) 

`checkPermissionOperations` simply tests one bit of a 256-bit capability bitmap against the numeric `ContractType`: [3](#0-2) 

Critically, `AccountPermissionUpdateContract` (`ContractType` id 46) is itself a member of the set of contract types that CAN be enabled in an Active permission's operations bitmap — confirmed by `AccountPermissionUpdateActuator.checkPermission()`, which validates the `operations` bytes against `DynamicPropertiesStore.getAvailableContractType()` (which includes id 46) rather than a permission-management-specific allow-list: [4](#0-3) 

Once an Owner has (for any reason — e.g., to let an operational/hot-wallet key rotate its own Active keys) enabled operation bit 46 on an Active permission, that Active key holder can sign a fresh `AccountPermissionUpdateContract` using its own low `permissionId`, and `execute()` will blindly replace the account's Owner permission, Witness permission, and entire Active permission list with attacker-chosen values — with no check that the signer's own permission tier is sufficient to touch Owner/Witness fields: [5](#0-4) 

This mirrors the Rancher `RoleTemplate` bug class (CVE-2023-32196/GHSA-64jq-m7rq-768h): a scoped/limited authorization construct (external `RoleTemplate` rules / a TRON Active permission) is treated by the enforcement layer as sufficient authority for an operation whose real blast radius (rewriting the entire permission hierarchy, including the highest-privilege Owner tier) far exceeds what the scoped grant was meant to convey, because the privilege-boundary check that should tie "what you may sign" to "what you may modify" is missing.

### Impact Explanation
Any account whose Owner has ever delegated the `AccountPermissionUpdateContract` capability bit to a lower-tier Active permission (a documented/intended TRON multisig feature, not inherently malicious) exposes that Active key holder to a full, permanent, unauthorized takeover of the account: the Active key holder can set itself as the sole Owner key with threshold 1, strip out the original Owner keys, and remove Witness permission — resulting in complete and irreversible loss of account control and any funds/assets/staked TRX/votes controlled by that account (CWE-269: Improper Privilege Management).

### Likelihood Explanation
Exploitation requires only a single, unprivileged, signed transaction from an account that already holds an Active permission with the `AccountPermissionUpdateContract` operation bit enabled (id 46). No consensus, network, or node compromise is needed — it is purely a signature/permission-validation logic gap reachable through the standard broadcast path (`Wallet.broadcastTransaction` → `TransactionCapsule.validatePubSignature` → `AccountPermissionUpdateActuator`). Because this capability bit is a normal, legitimately-grantable option in the `AccountPermissionUpdateContract` UI/SDK flow (it is part of `getAvailableContractType()`), any account owner who grants a narrower-seeming delegation is unknowingly granting full account takeover ability.

### Recommendation
- In `AccountPermissionUpdateActuator.validate()`/`execute()`, require that a transaction signed under a non-Owner (`permissionId != 0`) permission may only modify Active permissions with `id`/weight no greater than the signer's own permission, and must never be allowed to alter the Owner or Witness permission fields.
- Alternatively, exclude `AccountPermissionUpdateContract` from the set of contract types that can ever be enabled in an Active permission's `operations` bitmap (i.e., always require Owner permission, `permissionId == 0`, to execute this contract type), matching the "backing ClusterRole rules are the source of truth" remediation pattern used by the Rancher fix (introduce an explicit, restrictive resolution path instead of relying on a general capability bit).

### Proof of Concept
1. Owner account `A` creates an Active permission `P2` (id=2, threshold=1, single key `K`) and enables the `AccountPermissionUpdateContract` bit (id 46) in `P2.operations`, intending `K` to only rotate `P2`'s own keys, via a normal `AccountPermissionUpdateContract` signed with Owner permission.
2. Using only key `K`, craft a new `AccountPermissionUpdateContract` for account `A` with `Contract.Permission_id = 2`, setting:
   - `owner` = new Owner permission with `K` as sole key, threshold 1,
   - `actives` = empty or attacker-controlled list.
3. Sign with `K` only; `TransactionCapsule.validateSignature` passes because `P2` is Active type and has bit 46 set (`checkPermissionOperations` returns true) and `K`'s weight (1) meets `P2.threshold` (1).
4. Broadcast; `AccountPermissionUpdateActuator.execute()` (`actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java:49-51`) overwrites `A`'s Owner permission with the attacker-controlled one, giving `K` full, permanent Owner control of account `A`.

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
