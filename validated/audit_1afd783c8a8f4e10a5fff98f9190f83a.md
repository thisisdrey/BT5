## Analog Found

### Title
Permission `operations` allowlist authorization bypass via multi-contract transaction — signature/permission check validates only the first contract while all contracts in the transaction are executed - ([File: chainbase/src/main/java/org/tron/core/capsule/TransactionCapsule.java])

### Summary
The PraisonAI bug is a classic "check the first token, execute the whole string" allowlist bypass: `CommandValidator` authorizes only the first whitespace-delimited token, while `SandboxExecutor` runs the entire chained command through a shell. java-tron has a structurally identical pattern in its multisig "Active permission `operations`" authorization model: the permission/allowlist check (`checkPermissionOperations`) is evaluated against only the **first contract** in a transaction's contract list, while the executor (`ActuatorCreator` / `Manager`) builds and runs an actuator for **every** contract present in that same transaction.

### Finding Description
TRON's account permission system restricts a given `Active` permission key to a whitelist of contract types via a 256-bit `operations` bitmap (analogous to `allowedCommands`): [1](#0-0) 

This bitmap check is the sole authorization gate that restricts *which contract types* a signature under a given `permissionId` may authorize. It is invoked from `TransactionCapsule.validateSignature()`, `TransactionCapsule.addSign()`, and `TransactionCapsule.checkPermission()` — but in every case it is applied to a single, hard-coded contract index, `getContract(0)`, not to every contract carried by the transaction: [2](#0-1) [3](#0-2) 

The comment in `validateSignature()` explicitly documents the assumption that underlies this shortcut — "Do not support multi contracts in one transaction" — but nothing enforces or rejects a transaction that actually contains more than one contract: [4](#0-3) 

Meanwhile, the actual execution path is not scoped to `contract(0)`. `ActuatorCreator.createActuator()` iterates over the transaction's **entire** `getContractList()` and builds/executes an `Actuator` for each one: [5](#0-4) 

None of the per-contract `Actuator.validate()`/`execute()` implementations re-check the signer's `permission.getOperations()` bitmap against their own `ContractType` — that check only ever happens once, against `contract(0)`, at the transaction-level signature-verification stage (`preValidateTransactionSign` → `validateSignature`) shown earlier in `Manager.java`. This is the same "policy checked once against a prefix, then the full payload is executed" mismatch as the PraisonAI advisory: the allowlist authorizes one token/contract, but the executor treats the whole list as authorized.

### Impact Explanation
An account owner can create a restricted `Active` permission key whose `operations` bitmap only allows a narrow set of contract types (e.g., only `TransferContract`, as is standard for custodial "hot wallet" or automated signing keys). Because the authorization check binds to `contract(0)` only, a transaction signed by that restricted key can smuggle in additional contracts of types **not** present in the key's `operations` bitmap (e.g., `AccountPermissionUpdateContract`, `WithdrawBalanceContract`, `VoteWitnessContract`, `ProposalApproveContract`, `WitnessCreateContract`, exchange/market contracts, or additional `TransferContract`s to a different beneficiary). All of these will be executed by `Manager`/`ActuatorCreator` under the single signature that only satisfied the allowlist for the first contract. This is a concrete unauthorized account operation and potential theft/permanent freezing of funds (e.g., an `AccountPermissionUpdateContract` bundled in could hijack the account's ownership/permission structure entirely), which matches the "concrete unauthorized account operation, theft or permanent freezing of funds" bar.

### Likelihood Explanation
Reaching this requires only a single broadcastable transaction with a `raw_data.contract` list containing more than one `Contract` entry, signed by a key under a restricted `Active` permission whose `operations` allow contract[0]'s type but not the others' — no special privileges, node compromise, or multi-party collusion is required beyond possessing a restricted signing key (exactly the "unprivileged transaction broadcaster" reachable class). The gap is a design/implementation assumption ("single contract per tx") that is documented in a comment but not enforced anywhere in the validation path, making this readily reachable if multi-contract transactions are actually accepted by the node.

### Recommendation
- Enforce `getContractCount() == 1` at transaction-acceptance/validation time if multi-contract transactions are not intended to be supported, rejecting any transaction with more than one contract before it reaches `ActuatorCreator`.
- If multi-contract transactions must be supported, apply `checkPermissionOperations()` (and the full `checkPermission`/`checkWeight` logic) to **every** contract in the list, not just `contract(0)`, before executing any of them.
- Add regression tests proving a permission whose `operations` bitmap allows only `TransferContract` cannot get contracts of any other type executed, even when bundled alongside an allowed `TransferContract` in the same transaction.

### Proof of Concept
1. Owner account `A` creates an `Active` permission `P` (via `AccountPermissionUpdateContract`) with `operations` bitmap allowing only `TransferContract` (bit for `ContractType.TransferContract` set, all other bits clear), and assigns key `K` to `P`.
2. Craft `raw_data` with two contracts, both using `owner_address = A` and `Permission_id` referencing `P`:
   - `contract[0]`: `TransferContract` (small, allowed amount) — satisfies `checkPermissionOperations` in `TransactionCapsule.validateSignature()`.
   - `contract[1]`: `AccountPermissionUpdateContract` (or `WithdrawBalanceContract`/`VoteWitnessContract`) — a type NOT allowed by `P`'s `operations` bitmap.
3. Sign the whole transaction with `K` and broadcast it.
4. `TransactionCapsule.validateSignature()` only inspects `contract(0)` (`TransferContract`) and the signature weight is sufficient — the transaction is accepted.
5. `ActuatorCreator.createActuator()` builds actuators for **both** contracts; `Manager` executes both, so `contract[1]` (e.g., the permission update) executes despite `K`/`P` never being authorized for that contract type.

### Citations

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

**File:** chainbase/src/main/java/org/tron/core/capsule/TransactionCapsule.java (L698-719)
```java
  public boolean validateSignature(AccountStore accountStore,
      DynamicPropertiesStore dynamicPropertiesStore) throws ValidateSignatureException {
    if (!isVerified) {
      //Do not support multi contracts in one transaction
      Transaction.Contract contract = this.getInstance().getRawData().getContract(0);
      if (contract.getType() != ContractType.ShieldedTransferContract) {
        validatePubSignature(accountStore, dynamicPropertiesStore);
      } else {  //ShieldedTransfer
        byte[] owner = getOwnerAddress();
        if (!ArrayUtils.isEmpty(owner)) { //transfer from transparent address
          validatePubSignature(accountStore, dynamicPropertiesStore);
        } else { //transfer from shielded address
          if (this.transaction.getSignatureCount() > 0) {
            throw new ValidateSignatureException("there should be no signatures signed by "
                    + "transparent address when transfer from shielded address");
          }
        }
      }
      isVerified = true;
    }
    return true;
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ActuatorCreator.java (L40-58)
```java
  public List<Actuator> createActuator(TransactionCapsule transactionCapsule)
      throws ContractValidateException {
    List<Actuator> actuatorList = Lists.newArrayList();
    if (null == transactionCapsule || null == transactionCapsule.getInstance()) {
      logger.info("TransactionCapsule or Transaction is null");
      return actuatorList;
    }

    Protocol.Transaction.raw rawData = transactionCapsule.getInstance().getRawData();
    for (Contract contract : rawData.getContractList()) {
      try {
        actuatorList.add(getActuatorByContract(contract, transactionCapsule));
      } catch (Exception e) {
        logger.error("", e);
        throw new ContractValidateException(e.getMessage());
      }
    }
    return actuatorList;
  }
```
