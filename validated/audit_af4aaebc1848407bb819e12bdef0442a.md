This confirms the mechanism: `TransactionCapsule.validateSignature` and `validatePubSignature` verify a transaction's signatures against the account's *currently stored* `Permission` at the moment the transaction is processed [1](#0-0) , and `AccountPermissionUpdateActuator.execute` overwrites those permissions only when its own transaction is actually applied [2](#0-1) . There is no nonce/sequencing/expiration tie between a pending `AccountPermissionUpdateContract` and other pending transactions signed under the still-active (soon to be revoked) permission.

### Title
Race condition allows a soon-to-be-revoked permission key to front-run `AccountPermissionUpdateContract` and act after the owner believes it is disabled - (File: actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java)

### Summary
`AccountPermissionUpdateContract` lets an account owner replace its `owner`/`witness`/`active` `Permission` sets (e.g., to remove a compromised or otherwise untrusted key). The chain enforces permissions purely against whatever `Permission` is stored in `AccountStore` at the moment a transaction is executed [3](#0-2) . Because Tron, like other public blockchains, exposes pending transactions before they are confirmed, a holder of a key about to be removed can observe the owner's pending `AccountPermissionUpdateContract`, and race it with a competing, higher-fee/higher-priority transaction signed with the still-valid old key/permission. If that competing transaction is included first (or in the same block before the update is applied), it executes successfully under the permission the owner intended to revoke — exactly mirroring the whitelist `remove()`/`update()` race in the external report, where the actuator's `execute()` only takes effect for transactions ordered after it.

### Finding Description
- Permission enforcement is entirely state-based and has no "pending revocation" or invalidation window: `validateSignature`/`validatePubSignature` fetch `account.getPermissionById(permissionId)` fresh from `AccountStore` for each transaction and check `checkWeight` against whatever is currently persisted [1](#0-0) .
- `AccountPermissionUpdateActuator.execute` performs the actual permission replacement (`account.updatePermissions(...)`) only inside its own `execute()` call, i.e., only once that specific transaction is applied to the state [2](#0-1) .
- Any other transaction signed under the old permission and broadcast/gossiped before the update transaction is applied will still pass signature/permission validation as long as it is processed (mined) earlier — this is a pure ordering race, not a state-machine safeguard.
- This structurally matches the external report's `Whitelist.remove/update` bug class: an owner-level "revoke access" action is rendered ineffective if the target of the revocation manages to get a competing transaction processed first.

### Impact Explanation
If an account owner removes a compromised/untrusted key from `owner`, `active`, or `witness` permissions (the standard remediation for a leaked key), the holder of that key can, during the race window before the update transaction is confirmed, broadcast and get mined a transaction (e.g., `TransferContract`, `TransferAssetContract`, `TriggerSmartContract`, or even another `AccountPermissionUpdateContract` re-instating themselves) using the still-valid old permission. This can result in unauthorized transfer/theft of funds or unauthorized account control, defeating the very purpose of the permission-revocation mechanism.

### Likelihood Explanation
Reachable by any account holder using multi-sig (`AllowMultiSign`) with no special node privileges — it only requires broadcasting ordinary signed transactions via the public API, matching an "unprivileged transaction broadcaster" threat model. It requires the attacker to actively monitor the mempool/pending transactions and win a fee/ordering race, which is feasible but not guaranteed (front-running/MEV-style difficulty), consistent with the "High difficulty" rating given in the original report for the analogous Whitelist bug.

### Recommendation
Because signature/permission validation is inherently state-snapshot based, consider: (1) adding a monotonically increasing "permission version"/nonce to `Permission`/`AccountCapsule` that must be included and checked in transactions signed under a given permission set, so that superseded permissions cannot be used once a newer version exists in a later block; or (2) documenting and encouraging owners to first `pause`-equivalent (e.g., temporarily blocking outgoing transfers) or using a priority/atomic mechanism (e.g., bundling the permission update with dependent restrictions) before broadcasting a permission update, similar to the mitigation suggested in the original report.

### Proof of Concept
1. Owner account `A` has `AllowMultiSign` enabled with active-permission key `K` (weight sufficient to reach threshold), used for daily transfers.
2. `K` is discovered to be compromised (or the delegate holding `K` becomes untrusted). `A` broadcasts `AccountPermissionUpdateContract` to replace the active permission's key list, removing `K`, via `AccountPermissionUpdateActuator` [4](#0-3) .
3. The holder of `K` observes this pending transaction (e.g., via mempool/API), and immediately broadcasts a `TransferContract` (or `TriggerSmartContract`) signed with `K`, paying a higher energy/bandwidth priority so it is included in an earlier block/position.
4. Because `TransactionCapsule.validateSignature` checks against the `Permission` currently stored for `A` at the time each transaction is processed [5](#0-4) , the attacker's transaction succeeds using the still-active old permission, moving funds out of `A` before the revocation takes effect — the same outcome the external Whitelist `remove()` race achieves.

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
