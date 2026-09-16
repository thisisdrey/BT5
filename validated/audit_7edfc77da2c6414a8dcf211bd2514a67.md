### Title
Missing scope-containment check in `AccountPermissionUpdateContract` allows a low-privilege Active-permission key to self-escalate account permissions - (File: `actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java`)

### Summary
`AccountPermissionUpdateActuator` lets any account permission whose operations bitmap authorizes the `AccountPermissionUpdateContract` type (bit 46) submit a transaction that rewrites the account's `Owner`, `Witness`, and `Active` permissions. The actuator validates only structural properties of the new permissions (key count, threshold, address validity, valid contract-type bits) but never checks that the new operations/keys being granted are a subset of the scope already held by the permission that is signing the transaction. This mirrors the OpenClaw `node.pair.approve` flaw: an approver/operator can grant privileges broader than what it itself holds.

### Finding Description
`AccountPermissionUpdateActuator.checkPermission()` performs only structural validation of each submitted `Permission` (key count vs `getTotalSignNum()`, threshold > 0, distinct/valid addresses, weight sum, and — for `Active` permissions — that operation bits correspond to a "valid" `ContractType`): [1](#0-0) 

Separately, when a transaction is signed with a non-owner permission (`permissionId != 0`), the only authorization check performed is that the *signer's own* permission has the operations bit set for the *current* contract type being executed — i.e., for `AccountPermissionUpdateContract` itself: [2](#0-1) [3](#0-2) 

Nowhere in this path is the *content* of the new permissions being written (arbitrary key sets, arbitrary operations bitmaps for new `Active` permissions, or modifications to the `Owner` permission) checked against the scope already held by the signing key/permission. A key that is only delegated the narrow ability to "manage account permissions" can therefore use that single capability to rewrite the account's `Owner` permission or create a new `Active` permission with the full operations bitmap (all contract types, including `TransferContract`, `WitnessCreateContract`, `FreezeBalanceContract`, etc.), for itself or a colluding address — a straight privilege-escalation path with no containment check, exactly the CWE-863 pattern described in the advisory.

### Impact Explanation
An account owner who delegates a restricted `Active` permission (e.g., "can only submit permission-housekeeping transactions") to an operator/employee/automation key inadvertently grants that key the ability to seize full control of the account: rewrite `Owner` permission keys/threshold, or mint a new `Active` permission with unrestricted operations. This is a concrete unauthorized account-takeover / privilege-escalation vector reachable purely from a single signed transaction, potentially leading to theft of funds, unauthorized transfers, asset issuance, freezing/voting, or witness creation using the victim account's balance and TRX/TRC10/frozen resources.

### Likelihood Explanation
Exploitation requires only that a victim account has previously set up multisig with a low-privilege `Active` permission whose operations bitmap includes `AccountPermissionUpdateContract` (bit 46) — a legitimate and plausible configuration for delegating "permission management" duties. No collusion from validators/witnesses is needed; the attacker (holder of the low-privilege key) can unilaterally broadcast the malicious `AccountPermissionUpdateContract` transaction.

### Recommendation
In `AccountPermissionUpdateActuator.validate()` / `checkPermission()`, when the transaction is authorized via a non-owner (`Active`) permission (`permission_id != 0`), enforce that:
- the operations bitmap of any new `Active` permission being created/modified is a subset of the operations bitmap of the signing permission, and
- the signing (non-owner) permission cannot be used at all to modify the `Owner` permission or to create/modify permissions with a broader operations bitmap than itself (i.e., require `Owner`-permission (`permission_id == 0`) authorization for any privilege-escalating change).

### Proof of Concept
1. Owner `O` creates an `Active` permission `P1` for key `K1` via a legitimate `AccountPermissionUpdateContract`, with operations bitmap containing only bit 46 (`AccountPermissionUpdateContract`) — intended to let `K1` manage permissions only.
2. Holder of `K1` crafts and signs (using `permission_id = P1.id`) a new `AccountPermissionUpdateContract` that sets:
   - a new `Active` permission with operations bitmap = all bits set (full contract-type coverage), controlled by an attacker-owned key `K2`.
3. `TransactionCapsule.checkPermission()`/`checkPermissionOperations()` only verifies `P1` has bit 46 set for the currently-executing contract type (`AccountPermissionUpdateContract`) — it passes.
4. `AccountPermissionUpdateActuator.validate()`/`checkPermission()` only structurally validates the new `Active` permission (valid keys, threshold, valid bitmap) — it passes, with no check that `K2`'s new capabilities exceed `K1`'s own scope.
5. `execute()` commits the new permission to `AccountCapsule`, giving `K2` full transfer/freeze/vote/witness capability on account `O`'s funds and resources — full account compromise from a key that was meant to be scoped to permission-housekeeping only. [4](#0-3)

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L41-69)
```java
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

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L71-146)
```java
  private boolean checkPermission(Permission permission) throws ContractValidateException {
    DynamicPropertiesStore dynamicStore = chainBaseManager.getDynamicPropertiesStore();
    if (permission.getKeysCount() > dynamicStore.getTotalSignNum()) {
      throw new ContractValidateException("number of keys in permission should not be greater "
          + "than " + dynamicStore.getTotalSignNum());
    }
    if (permission.getKeysCount() == 0) {
      throw new ContractValidateException("key's count should be greater than 0");
    }
    if (permission.getType() == PermissionType.Witness && permission.getKeysCount() != 1) {
      throw new ContractValidateException("Witness permission's key count should be 1");
    }
    if (permission.getThreshold() <= 0) {
      throw new ContractValidateException("permission's threshold should be greater than 0");
    }
    String name = permission.getPermissionName();
    if (!StringUtils.isEmpty(name) && name.length() > 32) {
      throw new ContractValidateException("permission's name is too long");
    }
    //check owner name ?
    if (permission.getParentId() != 0) {
      throw new ContractValidateException("permission's parent should be owner");
    }

    long weightSum = 0;
    List<ByteString> addressList = permission.getKeysList()
        .stream()
        .map(x -> x.getAddress())
        .distinct()
        .collect(toList());
    if (addressList.size() != permission.getKeysList().size()) {
      throw new ContractValidateException(
          "address should be distinct in permission " + permission.getType());
    }
    for (Key key : permission.getKeysList()) {
      if (!DecodeUtil.addressValid(key.getAddress().toByteArray())) {
        throw new ContractValidateException("key is not a validate address");
      }
      if (key.getWeight() <= 0) {
        throw new ContractValidateException("key's weight should be greater than 0");
      }
      try {
        weightSum = addExact(weightSum, key.getWeight());
      } catch (ArithmeticException e) {
        throw new ContractValidateException(e.getMessage());
      }
    }
    if (weightSum < permission.getThreshold()) {
      throw new ContractValidateException(
          "sum of all key's weight should not be less than threshold in permission " + permission
              .getType());
    }

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
