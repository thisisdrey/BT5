### Title
Privilege escalation to full account Owner via a delegated Active permission executing `AccountPermissionUpdateContract` - ([File: actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java])

### Summary
An account owner can delegate a narrow, weaker `Active` permission to a co-signer/delegate key and grant it the `AccountPermissionUpdateContract` operation bit (a bit that the protocol explicitly allows to be included in an `Active` permission's operations bitmap). Because neither the transaction-level permission check nor the actuator's `validate()`/`execute()` logic constrains what the *new* owner/witness/active permission structure may contain relative to the signer's own (lower) privilege level, a holder of that single `Active` key can unilaterally rewrite the account's `Owner` permission — setting themselves as sole owner with threshold 1 — and permanently strip out the true owner and any other co-signers. This is a direct structural analog of CVE-2018-1000133: a lower-privileged principal editing a self-referential "permission" record to grant itself the highest privilege level, with no independent authorization gate.

### Finding Description
The permission model for a given account is stored as `owner_permission` (id 0), `witness_permission` (id 1) and `active_permission` (id ≥2), see the `Permission` message [1](#0-0) . A transaction that is not signed under the implicit Owner permission carries a `permission_id` referencing one of the account's `Active` permissions [2](#0-1) .

When a transaction executes under a non-zero `permissionId`, the only gate applied is that the permission must be of type `Active` and that the specific `contractType` bit is set in that permission's `operations` bitmap: [3](#0-2) 

This check is reused identically in signing (`addSign`), in full signature validation (`validateSignature`), in `Wallet.getTransactionApprovedList`, and in `TransactionUtil.getTransactionSignWeight`: [4](#0-3) [5](#0-4) 

Critically, `AccountPermissionUpdateContract` (contract type 46) is *not* excluded from the set of "available" contract types that may legally be turned on in an `Active` permission's operations bitmap — it is only excluded from the *default* active operations template. This is demonstrated by the actuator's own test suite, where `checkAvailableContractType` (the actual gate enforced in `checkPermission`) includes `AccountPermissionUpdateContract`, while a separate, unused `checkActiveDefaultOperations` value excludes it: [6](#0-5) 

The actuator that processes `AccountPermissionUpdateContract` (`AccountPermissionUpdateActuator.validate()`) validates the *shape* of the submitted `owner`/`witness`/`active` permissions (key counts, weights, thresholds, operation-bit legality against the global available-contract-type list) but never checks the *identity or privilege level of the signer/permissionId* that is invoking the update against the content being written: [7](#0-6) 

The result: once an owner grants any `Active` permission the `AccountPermissionUpdateContract` bit (e.g. intending a delegate to help rotate a subset of active keys), that delegate can single-handedly (using only their own Active key, satisfying only that Active permission's threshold — which can be as low as weight 1/threshold 1) submit a new `AccountPermissionUpdateContract` that:
- Sets a brand-new `Owner` permission consisting solely of the delegate's own address with threshold 1 (satisfying `checkPermission`'s only requirements: ≥1 key, weight sum ≥ threshold, valid address).
- Optionally clears/rewrites `Active` permissions to remove all other co-signers.

`updatePermissions` unconditionally overwrites the account's owner/witness/active permission fields with whatever was supplied in the contract, with no cross-check against the previous owner or the signer's original scope: [8](#0-7) 

This lets a deliberately weak, narrowly-scoped `Active` co-signer permanently and unilaterally seize full `Owner` control of the account — exactly the "unprivileged user sets 'System Administrator = yes' on themselves via a self-editable permission record with no independent authorization" pattern described in CVE-2018-1000133.

### Impact Explanation
Successful exploitation results in complete, permanent account takeover: the attacker (holder of a delegated Active key) becomes sole Owner of the victim account, and the legitimate owner and any other co-signers are locked out. Since Owner permission controls all contract types by default, the attacker gains unrestricted control over the account's funds, TRX/TRC10/TRC20 balances, staked/delegated resources, and any smart-contract-owner privileges tied to that account — this constitutes unauthorized account operation and theft/permanent freezing of funds for the original owner.

### Likelihood Explanation
Exploitation requires that the account owner has previously created an `Active` permission that includes the `AccountPermissionUpdateContract` bit for a co-signer/delegate (a configuration the protocol explicitly permits and that legitimate multi-sig/delegation setups may plausibly use, e.g. for automated key rotation services). Given that setup, exploitation is a single, self-signed, fully-valid transaction with no additional barrier — trivially reachable from any account holding such a delegated key.

### Recommendation
In `AccountPermissionUpdateActuator.validate()`/`execute()`, when the transaction is authorized via a non-Owner (`Active`) `permission_id`, restrict what may be written to the account's `owner_permission`/`witness_permission`/`active_permission` fields: e.g. disallow modifying `owner_permission` entirely unless the transaction is authorized under the Owner permission (`permissionId == 0`), or require that any new `Owner` permission still contain the pre-existing owner key(s)/threshold. Additionally, reconsider whether `AccountPermissionUpdateContract` should ever be an eligible bit for delegated `Active` permissions given its ability to fully rewrite account authority.

### Proof of Concept
1. Account `A` (owner key `K_owner`) creates an `Active` permission `P2` for delegate key `K_delegate`, with `operations` bit for `AccountPermissionUpdateContract` (type 46) set — allowed per `checkPermission`'s available-contract-type check [9](#0-8) .
2. `K_delegate` crafts and self-signs an `AccountPermissionUpdateContract` with `permission_id = 2` (referencing `P2`), setting:
   - `owner` = new `Permission{type=Owner, threshold=1, keys=[{address=K_delegate, weight=1}]}`
   - `actives` = `[{type=Active, threshold=1, operations=<all-zero or full>, keys=[{address=K_delegate, weight=1}]}]`
3. Broadcast the transaction. `checkPermission` (TransactionCapsule) passes because `permission_id=2` is Active and the `AccountPermissionUpdateContract` bit is set in `P2.operations`; `checkWeight` passes trivially (1 signature from `K_delegate` meeting `P2`'s threshold).
4. `AccountPermissionUpdateActuator.validate()`/`execute()` accepts the new `owner`/`active` permissions unconditionally (only structural checks apply) and calls `AccountCapsule.updatePermissions`, overwriting `owner_permission` to `K_delegate` alone.
5. `K_owner` and any other original signers are now permanently locked out; `K_delegate` is sole Owner of account `A`.

### Citations

**File:** protocol/src/main/protos/core/Tron.proto (L261-274)
```text
message Permission {
  enum PermissionType {
    Owner = 0;
    Witness = 1;
    Active = 2;
  }
  PermissionType type = 1;
  int32 id = 2; //Owner id=0, Witness id=1, Active id start by 2
  string permission_name = 3;
  int64 threshold = 4;
  int32 parent_id = 5;
  bytes operations = 6; //1 bit 1 contract
  repeated Key keys = 7;
}
```

**File:** Tron protobuf protocol document.md (L846-846)
```markdown
  `Permission_id`: for multisign, the value is in [0, 9], 0 is owner，1 is witness, 2-9 is active.
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

**File:** framework/src/main/java/org/tron/core/Wallet.java (L665-678)
```java
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
