### Title
Single-Step, Unconfirmed Owner/Active Permission Replacement in `AccountPermissionUpdateActuator` Can Permanently Lock an Account Out of Its Own Funds - (File: `actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java`)

### Summary
`AccountPermissionUpdateContract` lets any account holder overwrite their `Owner`, `Witness`, and `Active` permissions in a single, irrevocable step. There is no "pending owner" staging or acceptance step analogous to the two-step ownership-transfer pattern recommended in the source report. A single malformed/incorrect transaction immediately and permanently replaces the account's permission set with no recovery path.

### Finding Description
`AccountPermissionUpdateActuator.execute()` unpacks the incoming `AccountPermissionUpdateContract` and calls `AccountCapsule.updatePermissions()` directly, which unconditionally overwrites the account's owner/witness/active permission fields in one atomic write: [1](#0-0) 

The overwrite logic itself performs no round-trip verification that the new keys are controllable — it simply replaces the previous permission structure: [2](#0-1) 

`validate()` only checks structural constraints (key count, threshold arithmetic, distinctness, operations bitmap, etc.) — it never verifies that the newly specified keys/addresses are reachable/controllable by the submitter, nor does it require any subsequent "accept" transaction from the new key holders: [3](#0-2) 

This contract is reachable directly from the public HTTP API without any additional confirmation step, via `AccountPermissionUpdateServlet.doPost`, which builds and broadcasts the transaction straight from client-supplied JSON: [4](#0-3) 

This is structurally the same bug class as the referenced `OwnableImpl.transferOwnership` finding: a critical, security-relevant address/permission field is swapped in a single step, with no staged "pendingOwner"-style confirmation, so a typo, malformed address, or unintended key set is applied immediately and cannot be reversed by the account.

### Impact Explanation
Because the `Owner` permission gates the ability to issue further `AccountPermissionUpdateContract` transactions, and the `Active` permission list governs which keys can move assets (TRX/TRC10/TRC20 transfers, resource operations, etc.), an erroneous one-shot update (wrong address, mistyped key, threshold set unreachable by any held key) permanently locks the account out of its own permission-management and fund-movement capabilities. There is no owner-side rollback, and no counterpart contract type exists to "revert" a bad `AccountPermissionUpdateContract`. This results in permanent freezing of the account's funds.

### Likelihood Explanation
This requires only a single valid, self-signed transaction from the account owner (or any key meeting the account's current threshold) — reachable directly through the HTTP/gRPC transaction-creation and broadcast path (`AccountPermissionUpdateServlet` / `Wallet.createTransactionCapsule`), with no privileged network role needed. Malformed or incorrect permission data (address typos, wrong key generation, excessive/insufficient threshold relative to held keys) is a realistic operational hazard for wallets, multisig setups, and automated tooling constructing this contract type.

### Recommendation
Introduce a staged confirmation flow for permission overwrites, analogous to a two-step ownership transfer:
- On submission of `AccountPermissionUpdateContract`, store the proposed permission set as "pending" rather than applying it immediately.
- Require a follow-up transaction signed under the *new* keys (meeting the new threshold) to "accept" and commit the pending permission set, mirroring `acceptOwnership()` in the referenced pattern.
- Alternatively, require the transaction to include a signature proof from at least one new key in addition to the current threshold, ensuring the new keys are actually controllable before the swap takes effect.

### Proof of Concept
1. Account `A` (holding TRX/TRC10/TRC20 balances) submits an `AccountPermissionUpdateContract` via `AccountPermissionUpdateServlet.doPost` (or gRPC equivalent), specifying a new `Owner` permission and `Active` permission list.
2. Due to a typo in one of the key addresses or an incorrect threshold value relative to actually-held keys, `AccountPermissionUpdateActuator.validate()` passes (all structural checks are satisfied) and `execute()` commits the change via `AccountCapsule.updatePermissions()`.
3. Account `A` no longer holds any key set that meets the new `Owner`/`Active` thresholds.
4. Account `A` can never again submit a valid `AccountPermissionUpdateContract` (to fix the mistake) nor any `TransferContract`/asset-moving transaction requiring `Active` permission — its funds are permanently frozen with no recovery mechanism in the protocol.

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

**File:** framework/src/main/java/org/tron/core/services/http/AccountPermissionUpdateServlet.java (L26-41)
```java
  protected void doPost(HttpServletRequest request, HttpServletResponse response) {
    try {
      PostParams params = PostParams.getPostParams(request);
      AccountPermissionUpdateContract.Builder build = AccountPermissionUpdateContract.newBuilder();
      JsonFormat.merge(params.getParams(), build, params.isVisible());

      Transaction tx = wallet
          .createTransactionCapsule(build.build(), ContractType.AccountPermissionUpdateContract)
          .getInstance();
      JSONObject jsonObject = JSONObject.parseObject(params.getParams());
      tx = Util.setTransactionPermissionId(jsonObject, tx);
      response.getWriter().println(Util.printCreateTransaction(tx, params.isVisible()));
    } catch (Exception e) {
      Util.processError(e, response);
    }
  }
```
