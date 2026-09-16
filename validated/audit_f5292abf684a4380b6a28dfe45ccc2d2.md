### Title
Multi-sig approval threshold/permission can be silently altered after signatures are collected but before broadcast, allowing under-approved transactions to execute - (File: chainbase/src/main/java/org/tron/core/capsule/TransactionCapsule.java)

### Summary
This is a structural analog of the Llama `LlamaCore` bug: an "action" (transaction) is approved by a set of signers under one set of rules (a `Permission`'s threshold/keys/allowed operations), but the actual rule enforced at execution time is fetched fresh from mutable on-chain state and can be changed by an unrelated transaction from a *different* signer set (the account's Owner permission) before the originally-approved transaction is broadcast, changing what "approved" effectively means for the pending transaction.

### Finding Description
Multi-sig transactions in java-tron reference a `permission_id` in `Transaction.Contract` rather than embedding the permission's keys/threshold/operations at signing time. Individual signers call `TransactionCapsule.addSign()` [1](#0-0) , which signs only the raw transaction hash and checks the *current* `Permission` fetched via `account.getPermissionById(permissionId)` merely to confirm the signer's own weight — it does not bind the collected signature set to a specific snapshot/version of the permission.

Final validation at broadcast time (`validatePubSignature` → `validateSignature` → `checkWeight`) again re-fetches the account's permission fresh from the `AccountStore` and checks the newly-fetched threshold/keys against the previously-collected signatures: [2](#0-1)  and [3](#0-2) .

Because the `Permission` (keys, weights, threshold, and allowed `operations` bitmap for the Active permission with a given `permission_id`) can be modified at any time by `AccountPermissionUpdateActuator` via a completely independent `AccountPermissionUpdateContract`, signed under the account's **Owner** permission (a different key set/threshold than the Active permission being modified) — see `checkPermission`/`validate`/`execute` [4](#0-3)  and [5](#0-4)  — the permission definition used to validate a pending, partially- or fully-signed transaction is not fixed at the time signers approve it.

Concretely: if an Active permission (e.g. `permission_id = 2`) initially requires threshold 3 across keys A, B, C, and signer A signs a transaction referencing that `permission_id`, the transaction cannot yet be broadcast (1 < 3). If, before broadcast, the account owner (using the Owner permission, a disjoint key set) issues an `AccountPermissionUpdateContract` that lowers the Active permission's threshold to 1 (while keeping key A in the key list), A's earlier signature — which is a valid signature over the raw transaction bytes and does not encode the permission version it was made under — now satisfies the new, lowered threshold. The transaction, originally intended to require 3-of-N approval, executes with only the 1 signature that was collected under the old, stricter rule. This mirrors the Llama issue where the actual "opcode"/execution semantics of an already-approved action is determined by external, independently-mutable state (`authorizedScripts`) that can be changed by a different set of signers without touching the action itself.

### Impact Explanation
This allows execution of a transaction (e.g., a `TransferContract`, `TriggerSmartContract`, or another sensitive contract type gated behind a high-threshold Active permission) with fewer or different real-world approvals than the multi-sig policy intended. In a treasury/custody setup relying on `AccountPermissionUpdateContract`-based thresholds, this can result in unauthorized transfer of funds or unauthorized invocation of privileged operations, since the "approval" collected under the old permission is retroactively reinterpreted under a new permission it was never actually validated against.

### Likelihood Explanation
Exploitation requires control of (or collusion by) the account owner to change the Active permission for the target `permission_id` while a multi-sig transaction with insufficient signatures is pending, or more subtly, requires that partial approvals are gathered off-chain over time (a realistic workflow, since `getTransactionSignWeight`/`getTransactionApprovedList` exist specifically to support incremental signature collection) [6](#0-5) . Because permission changes are a normal, permitted, unrelated operation (`AllowMultiSign` must simply be enabled) [7](#0-6) , this is a realistic misuse/race scenario rather than a purely theoretical one, though it does require either malicious/compromised owner-key behavior or a race between permission updates and pending multi-sig broadcasts — reducing likelihood to Medium.

### Recommendation
Bind signatures/approvals to an immutable snapshot of the permission (e.g., include a permission version/hash in the signed payload, or invalidate/require re-collection of signatures whenever `AccountPermissionUpdateContract` changes a permission referenced by pending, not-yet-broadcast transactions). At minimum, `checkWeight`/`validateSignature` should verify against the permission state that existed when the transaction was constructed (or reject if the referenced permission has since changed), not the permission state current at broadcast time.

### Proof of Concept
1. Owner creates account with Active permission `id=2`, threshold=3, keys = {A:1, B:1, C:1}.
2. Signer A calls `addSign` on transaction TX (referencing `permission_id=2`); current weight = 1 < 3, so TX cannot broadcast yet.
3. Before TX is broadcast, the account Owner (using the Owner permission, different key set) submits and executes an `AccountPermissionUpdateContract` that redefines Active permission `id=2` to threshold=1, keys = {A:1} (dropping B, C).
4. TX, still carrying only A's original signature, is now broadcast. `validatePubSignature` → `validateSignature` fetches the *new* permission (threshold 1) and `checkWeight` returns weight=1 ≥ threshold=1, so the transaction succeeds and executes with only 1 of the originally-required 3 approvals.

### Citations

**File:** chainbase/src/main/java/org/tron/core/capsule/TransactionCapsule.java (L233-270)
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

**File:** chainbase/src/main/java/org/tron/core/capsule/TransactionCapsule.java (L597-633)
```java
  public void addSign(byte[] privateKey, AccountStore accountStore)
      throws PermissionException, SignatureException, SignatureFormatException {
    Transaction.Contract contract = this.transaction.getRawData().getContract(0);
    int permissionId = contract.getPermissionId();
    byte[] owner = getOwnerAddress();
    AccountCapsule account = accountStore.get(owner);
    if (account == null) {
      throw new PermissionException("Account is not exist!");
    }
    Permission permission = account.getPermissionById(permissionId);
    if (permission == null) {
      throw new PermissionException("permission isn't exit");
    }
    checkPermission(permissionId, permission, contract);
    List<ByteString> approveList = new ArrayList<>();
    SignInterface cryptoEngine = SignUtils
        .fromPrivate(privateKey, CommonParameter.getInstance().isECKeyCryptoEngine());
    byte[] address = cryptoEngine.getAddress();
    if (this.transaction.getSignatureCount() > 0) {
      checkWeight(permission, this.transaction.getSignatureList(),
          this.getTransactionId().getBytes(),
          approveList);
      if (approveList.contains(ByteString.copyFrom(address))) {
        throw new PermissionException(encode58Check(address) + " had signed!");
      }
    }

    long weight = getWeight(permission, address);
    if (weight == 0) {
      throw new PermissionException(
          ByteArray.toHexString(privateKey) + "'s address is " + encode58Check(address)
              + " but it is not contained of permission.");
    }
    ByteString sig = ByteString.copyFrom(cryptoEngine.Base64toBytes(cryptoEngine
        .signHash(getTransactionId().getBytes())));
    this.transaction = this.transaction.toBuilder().addSignature(sig).build();
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

**File:** actuator/src/main/java/org/tron/core/utils/TransactionUtil.java (L199-276)
```java
  public TransactionSignWeight getTransactionSignWeight(Transaction trx) {
    TransactionSignWeight.Builder tswBuilder = TransactionSignWeight.newBuilder();
    Result.Builder resultBuilder = Result.newBuilder();
    if (trx.getSignatureCount() > chainBaseManager.getDynamicPropertiesStore()
        .getTotalSignNum()) {
      resultBuilder.setCode(Result.response_code.OTHER_ERROR);
      resultBuilder.setMessage("too many signatures");
      tswBuilder.setResult(resultBuilder);
      return tswBuilder.build();
    }

    trx = truncateSignatures(trx);
    TransactionExtention.Builder trxExBuilder = TransactionExtention.newBuilder();
    trxExBuilder.setTransaction(trx);
    trxExBuilder.setTxid(ByteString.copyFrom(Sha256Hash.hash(CommonParameter
        .getInstance().isECKeyCryptoEngine(), trx.getRawData().toByteArray())));
    Return.Builder retBuilder = Return.newBuilder();
    retBuilder.setResult(true).setCode(response_code.SUCCESS);
    trxExBuilder.setResult(retBuilder);
    tswBuilder.setTransaction(trxExBuilder);

    if (trx.getRawData().getContractCount() == 0) {
      resultBuilder.setCode(Result.response_code.OTHER_ERROR);
      resultBuilder.setMessage("Invalid transaction: no valid contract");
    } else {
      try {
        Contract contract = trx.getRawData().getContract(0);
        byte[] owner = TransactionCapsule.getOwner(contract);
        AccountCapsule account = chainBaseManager.getAccountStore().get(owner);
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
        if (trx.getSignatureCount() > 0) {
          List<ByteString> approveList = new ArrayList<>();
          long currentWeight = TransactionCapsule.checkWeight(permission, trx.getSignatureList(),
              Sha256Hash.hash(CommonParameter.getInstance()
                  .isECKeyCryptoEngine(), trx.getRawData().toByteArray()), approveList);
          tswBuilder.addAllApprovedList(approveList);
          tswBuilder.setCurrentWeight(currentWeight);
        }
        if (tswBuilder.getCurrentWeight() >= permission.getThreshold()) {
          resultBuilder.setCode(Result.response_code.ENOUGH_PERMISSION);
        } else {
          resultBuilder.setCode(Result.response_code.NOT_ENOUGH_PERMISSION);
        }
      } catch (SignatureFormatException signEx) {
        resultBuilder.setCode(Result.response_code.SIGNATURE_FORMAT_ERROR);
        resultBuilder.setMessage(signEx.getMessage());
      } catch (SignatureException signEx) {
        resultBuilder.setCode(Result.response_code.COMPUTE_ADDRESS_ERROR);
        resultBuilder.setMessage(signEx.getMessage());
      } catch (PermissionException permEx) {
        resultBuilder.setCode(Result.response_code.PERMISSION_ERROR);
        resultBuilder.setMessage(permEx.getMessage());
      } catch (Exception ex) {
        resultBuilder.setCode(Result.response_code.OTHER_ERROR);
        resultBuilder.setMessage(ex.getClass() + " : " + ex.getMessage());
      }
    }

    tswBuilder.setResult(resultBuilder);
    return tswBuilder.build();
  }
```
