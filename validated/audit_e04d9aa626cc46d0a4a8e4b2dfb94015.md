## Title
Owner Permission Can Race-Condition Invalidate Active-Permission Delegate Transactions via `AccountPermissionUpdateContract` - (File: `actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java`)

### Summary
TRON's multi-signature permission system lets an account's Owner permission unilaterally rewrite the account's `Active` permission (keys, weights, threshold, and the allowed-operations bitmap) at any time via `AccountPermissionUpdateContract`. Because signature validation for any transaction is re-evaluated against the *current* on-chain permission state at execution time (not the state at signing time), the Owner can front-run an already-signed, in-flight transaction from an Active-permission delegate by submitting a permission update that invalidates the delegate's signature weight or revokes the operation, then back-run with a conflicting transaction of their own — exactly mirroring the "trusted forwarder" race-condition pattern from the Teller report, where a privileged address (market owner / account Owner permission) can change the authorization gate (trusted forwarder / Active permission) that another party's pre-signed transaction depends on.

### Finding Description
`AccountPermissionUpdateActuator.execute()` immediately overwrites the account's `Owner`, `Witness`, and `Active` permissions with no delay or timelock: [1](#0-0) 

Any subsequently-processed transaction that references a `permission_id` is checked against the *live* `AccountStore` state, not the permission state that existed when the transaction was signed: [2](#0-1) 

The weight/threshold check (`checkWeight`) similarly operates on the current permission object supplied to it: [3](#0-2) 

Wallet-side pre-checks used by clients/relayers to estimate whether a partially-collected multisig transaction has "enough permission" (`getTransactionSignWeight`, `getTransactionApprovedList`) suffer the same time-of-check/time-of-use gap — they read the permission at query time, but the transaction may not be broadcast/mined until later, during which the Owner can change the permission: [4](#0-3) [5](#0-4) 

This is directly analogous to the Teller bug: `setTrustedMarketForwarder` let a market owner instantly redirect the address gating validity of a user's meta-transaction, invalidating it before it landed. In java-tron, the account Owner permission gates the validity of any Active-permission delegate's transaction (via `permission_id` + operations bitmap check in `AccountPermissionUpdateActuator.checkPermission`), and can be changed unilaterally and instantly: [6](#0-5) 

### Impact Explanation
An Owner-permission holder (or a colluding subset reaching Owner threshold) can observe a pending, signed transaction from an Active-permission delegate in the mempool (e.g., a delegate attempting to cancel an order, withdraw funds, or perform a restricted operation on behalf of the account) and front-run it with an `AccountPermissionUpdateContract` that removes the delegate's key, lowers its weight below threshold, or strips the relevant bit from the `operations` bitmap. This deterministically causes the delegate's transaction to fail signature/permission validation (`PermissionException`/`ValidateSignatureException`) when it is later processed, while the Owner back-runs with a conflicting transaction that benefits them — griefing the delegate and potentially enabling unauthorized diversion of funds that the delegate's transaction was meant to protect or execute.

### Likelihood Explanation
This requires the attacker to already hold sufficient weight to satisfy the account's Owner permission threshold to submit `AccountPermissionUpdateContract`, which is a real, commonly-configured role for multisig/delegated accounts (e.g., an account owner delegating a subset of operations to a hot-wallet/relayer key via `Active` permission while retaining full unilateral Owner control). Any account using this delegation pattern for automated relaying, exchange hot-wallets, or shared custody is exposed, and the attack is purely a mempool-observation + front-run, requiring no special privileges beyond the Owner role itself.

### Recommendation
Consider decoupling "what permission is valid" at signing time from "what permission is valid" at execution time for already-broadcast transactions — e.g., by binding transactions to a permission version/nonce that must match at execution, or by introducing a delay before permission changes take effect (similar to the Sherlock report's suggestion to fix the forwarder only at creation time and never allow it to be silently changed for pending operations). At minimum, document this trust assumption clearly so integrators relying on `Active` permission delegation are aware that the Owner can invalidate in-flight delegate transactions at will.

### Proof of Concept
1. Account `A` has Owner permission held by key `K_owner` (threshold 1) and an Active permission (`id=2`) held by delegate key `K_delegate` authorized for `TransferContract` via the operations bitmap.
2. `K_delegate` signs and broadcasts a `TransferContract` (permission_id=2) to protect/move funds.
3. `K_owner`, observing this in the mempool, immediately submits `AccountPermissionUpdateContract` (processed by `AccountPermissionUpdateActuator`) that removes `K_delegate` from the Active permission or clears the relevant operation bit.
4. When `A`'s pending `TransferContract` is later validated in `TransactionCapsule.validateSignature` / `checkPermission`, it fails because `account.getPermissionById(2)` no longer matches or `checkPermissionOperations` rejects it — the delegate's transaction is dropped.
5. `K_owner` back-runs with a conflicting transaction (e.g., transferring the funds elsewhere), completing the griefing/race-condition attack.

### Citations

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

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L124-146)
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
  }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/TransactionCapsule.java (L233-269)
```java
  public static long checkWeight(Permission permission, List<ByteString> sigs, byte[] hash,
      List<ByteString> approveList)
      throws SignatureException, PermissionException, SignatureFormatException {
    long currentWeight = 0;
    if (sigs.size() > permission.getKeysCount()) {
      throw new PermissionException(
          "Signature count is " + (sigs.size()) + " more than key counts of permission : "
              + permission.getKeysCount());
    }
    HashMap addMap = new HashMap();
    for (ByteString sig : sigs) {
      if (sig.size() < 65) {
        throw new SignatureFormatException(
            "Signature size is " + sig.size());
      }
      String base64 = TransactionCapsule.getBase64FromByteString(sig);
      byte[] address = SignUtils
          .signatureToAddress(hash, base64, CommonParameter.getInstance().isECKeyCryptoEngine());
      long weight = getWeight(permission, address);
      if (weight == 0) {
        throw new PermissionException(
            ByteArray.toHexString(hash) + " is signed by " + encode58Check(address)
                + " but it is not contained of permission.");
      }
      if (ForkController.instance().pass(Parameter.ForkBlockVersionEnum.VERSION_4_7_1)) {
        base64 = encode58Check(address);
      }
      if (addMap.containsKey(base64)) {
        throw new PermissionException(encode58Check(address) + " has signed twice!");
      }
      addMap.put(base64, weight);
      if (approveList != null) {
        approveList.add(ByteString.copyFrom(address)); //out put approve list.
      }
      currentWeight += weight;
    }
    return currentWeight;
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

**File:** actuator/src/main/java/org/tron/core/utils/TransactionUtil.java (L228-245)
```java
        if (Objects.isNull(account)) {
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
          if (!checkPermissionOperations(permission, contract)) {
            throw new PermissionException("Permission denied!");
          }
        }
        tswBuilder.setPermission(permission);
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
