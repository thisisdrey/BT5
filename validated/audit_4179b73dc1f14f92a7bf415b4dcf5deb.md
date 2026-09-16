### Title
Missing safeguards in `AccountPermissionUpdateActuator` allow an account owner to irreversibly lock themselves out of their own account, permanently freezing all funds - (File: actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java)

### Summary
`AccountPermissionUpdateActuator` lets an account holder replace their `Owner`, `Witness`, and `Active` permissions in a single transaction with no restriction ensuring the account remains controllable afterward. Just like the referenced `auth.sol` bug where an owner could remove themselves from the admin role and brick the system, a java-tron account owner can submit a valid, fully-authorized `AccountPermissionUpdateContract` that sets every permission (`Owner` and all `Active` permissions) to keys/thresholds that can never be satisfied together, permanently freezing that account's TRX/TRC10/TRC20 balance, frozen resources, and voting rights, with no recovery path.

### Finding Description
The actuator's `checkPermission` only enforces internal structural validity of each new permission (key count bounds, distinct addresses, valid address format, positive weights, `weightSum >= threshold`): [1](#0-0) 

It never checks that the *new* permission set will still be reachable by keys the current owner actually controls, nor does it compare the new permission against the current one to prevent complete self-lockout. `execute()` then unconditionally overwrites the account's `Owner`, `Witness`, and `Active` permissions: [2](#0-1) 

`AccountCapsule.updatePermissions` performs the raw replacement with no additional guardrails: [3](#0-2) 

The transaction is authorized normally: signature verification (`TransactionCapsule.validateSignature`) checks the *current* Owner permission's threshold before the actuator runs, so the update itself is fully authorized by the current owner: [4](#0-3) 

Because `AccountPermissionUpdateContract` lets a single transaction set the `owner` permission and up to 8 `actives` permissions simultaneously (`account_contract.proto`): [5](#0-4) 

an owner (or a phishing dApp that convinces the owner to sign such a contract) can atomically replace both the Owner permission and every Active permission with keys/thresholds that no combination of controlled private keys can ever satisfy (e.g., threshold requires cooperation of two random unowned addresses). Since `TransferContract`, `TransferAssetContract`, `FreezeBalanceV2Contract`, etc. are authorized via the Active permission by default and the Owner permission via permission_id 0, once both are made unsatisfiable the account can never again produce a valid signature for any operation, including a subsequent `AccountPermissionUpdateContract` to fix itself.

This exactly mirrors the reported bug class: the "owner" is allowed, with no dedicated safety mechanism (two-step handoff, mandatory key retention, or restricted separate function), to strip themselves of the ability to operate the very account/role they administer.

### Impact Explanation
This results in permanent freezing of funds for any affected account: TRX balance, TRC10 tokens, frozen/staked TRX for bandwidth/energy, delegated resources, and voting power become permanently inaccessible once both Owner and all Active permissions are set to an unreachable configuration. There is no on-chain recovery mechanism once this occurs. This matches the "permanent freezing of funds" impact category.

### Likelihood Explanation
This requires either (a) a user error when configuring multisig permissions, or (b) social engineering / a malicious dApp that requests the account owner sign a `AccountPermissionUpdateContract` (via `AccountPermissionUpdateServlet`/JSON-RPC/gRPC `createTransactionCapsule` flow) that looks benign but sets unreachable keys/thresholds. Because the actuator performs no sanity check preventing this outcome, any successfully signed and broadcast transaction of this shape will succeed and immediately brick the account. [6](#0-5) 

### Recommendation
- Add a validation step in `AccountPermissionUpdateActuator.validate()` that rejects permission updates where the new `Owner` permission (and, if being changed, the Active permissions that would leave the account with no viable Owner/Active path) does not include at least one key whose weight alone, or combined with other kept/known keys, would still be able to reach the new threshold — at minimum, warn/reject configurations with zero overlap between old and new Owner permission keys unless a separate, explicit "escape hatch" (e.g., a two-step confirm-transfer via a fresh key, or a mandatory recovery key) is used.
- Consider disallowing an `AccountPermissionUpdateContract` from changing Owner and all Active permissions in the same transaction to a fully disjoint key set in one step; instead require a staged/timelocked update similar to a two-step ownership transfer pattern.

### Proof of Concept
1. Attacker (or a compromised/careless owner) constructs an `AccountPermissionUpdateContract` for `ownerAddress`:
   - `owner` = `Permission{type=Owner, threshold=2, keys=[ {addrX, weight=1}, {addrY, weight=1} ]}` where `addrX`/`addrY` are addresses the current owner does not control.
   - `actives` = single `Permission{type=Active, threshold=2, keys=[ {addrX, weight=1}, {addrY, weight=1} ], operations=<all system contracts>}`.
2. Sign and broadcast this transaction using the current Owner permission (permission_id=0), which passes `TransactionCapsule.validateSignature` since it is signed with the still-valid current key.
3. `AccountPermissionUpdateActuator.validate()` passes: each permission has `keysCount>0`, positive weights, `weightSum(2) >= threshold(2)`, valid addresses — no check ties the new keys back to keys the signer actually controls.
4. `execute()` calls `account.updatePermissions(...)`, overwriting Owner and Active permissions.
5. From this point on, no transaction from `ownerAddress` (Transfer, Freeze, further `AccountPermissionUpdateContract`, etc.) can ever collect a valid threshold of signatures, since `addrX`/`addrY` are not controlled by the original owner. All TRX/TRC10/TRC20 balances and resources held by the account are permanently frozen.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L41-52)
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
```

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L71-122)
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

**File:** protocol/src/main/protos/core/contract/account_contract.proto (L44-49)
```text
message AccountPermissionUpdateContract {
  bytes owner_address = 1;
  Permission owner = 2; //Empty is invalidate
  Permission witness = 3; //Can be empty
  repeated Permission actives = 4; //Empty is invalidate
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
