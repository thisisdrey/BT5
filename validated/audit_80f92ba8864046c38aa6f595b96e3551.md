Based on the investigation, I found a concrete analog to the reported access-control bug in java-tron's transaction-signature verification path.

### Title
Insufficient signature/owner verification for multi-contract transactions - (File: chainbase/src/main/java/org/tron/core/capsule/TransactionCapsule.java)

### Summary
The reported Solidity bug is a case where a batch operation checks authorization (`isApprovedOrOwner`) only for the *first* item in a list, then blindly trusts the rest. The java-tron protocol layer contains a structurally identical pattern: a `Transaction` can carry multiple `Contract` entries in `rawData.getContractList()`, but the core signature-validation routine `TransactionCapsule.validateSignature()` only inspects and verifies the owner/permission/signature of `getContract(0)` — the first contract — while `ActuatorCreator.createActuator()` builds and executes an `Actuator` for *every* contract in the list, regardless of whose `ownerAddress` each one carries.

### Finding Description
`TransactionCapsule.validateSignature()` explicitly takes only the first contract: [1](#0-0) 

It even carries the comment "Do not support multi contracts in one transaction", showing the developers assumed (rather than strictly enforced) a single-contract-per-transaction invariant. The single-contract owner/permission/weight check is performed via: [2](#0-1) 

Meanwhile, `ActuatorCreator.createActuator()` iterates over **all** contracts contained in the raw transaction and builds an actuator for each one to be executed against chain state: [3](#0-2) 

Each actuator's own `validate()`/`execute()` (e.g. `TransferAsset`, `WithdrawBalanceContract`, `VoteWitnessActuator`, etc.) reads the `ownerAddress` field embedded in that specific contract's protobuf payload and operates against that account — it does **not** re-verify that the transaction's signature(s) correspond to that particular contract's owner; that check was supposed to happen once, up-front, in `validateSignature()`, which only covers `contract(0)`.

This is the same "check the first element, trust the rest" pattern that the InfinityPools report describes for `batchActionsOnSwappers()`: authorization is validated once for the first list entry, and every other entry in the list is processed under the assumption that the same authorization applies, without an equivalent per-entry check.

### Impact Explanation
If a transaction with multiple `Contract` entries reaches `ActuatorCreator`/`Manager` execution (i.e., is not otherwise rejected for having more than one contract before actuator dispatch), an attacker could craft `contract(0)` with their own address and valid signature to pass `validateSignature()`, while including subsequent contracts whose `ownerAddress` fields belong to a victim account (e.g., a `TransferContract`, `WithdrawBalanceContract`, `VoteWitnessContract`, or `UnfreezeBalanceV2Contract`). Because only `contract(0)`'s owner/signature is checked, those subsequent contracts would be executed by their respective actuators using the victim's balance/resources/votes without the victim ever having signed for those specific operations — resulting in unauthorized account operations and potential theft of funds or resources.

### Likelihood Explanation
This is reachable directly from a single signed transaction broadcast by any unprivileged network participant, requiring no special privilege — matching the required "unprivileged transaction broadcaster" threat model. The likelihood hinges entirely on whether an earlier, unverified validation step in the transaction-acceptance pipeline (e.g. `Manager`, `TransactionUtil`, or protobuf schema/business-logic checks prior to `ActuatorCreator`) enforces `getContractCount() == 1`. I was not able to conclusively locate such an enforced rejection within the available indexed code — I found only a defensive assertion in test utilities (`TransactionUtils.validTransaction`, which is test-only code) and a comment acknowledging the "no multi-contract support" assumption, but no hard runtime rejection of `Transaction.raw` instances with more than one contract prior to actuator creation and execution.

### Recommendation
Explicitly reject any `Transaction` whose `rawData.getContractCount() != 1` at the earliest possible validation point (before `TransactionCapsule.validateSignature()` and before `ActuatorCreator.createActuator()`), or alternatively, extend `validateSignature()`/`checkWeight()` to independently verify the owner, permission, and signature weight for **every** contract in `getContractList()`, not just `getContract(0)`.

### Proof of Concept
Conceptual reproduction (pending confirmation that no earlier check exists):
1. Attacker builds a `Transaction.raw` with two contracts: `contract[0]` = a low-value `TransferContract` from the attacker's own account (so `validateSignature()` succeeds using the attacker's real signature), and `contract[1]` = a `WithdrawBalanceContract`/`TransferContract`/`VoteWitnessContract` whose `ownerAddress` is a victim account the attacker does not control.
2. Attacker signs the transaction only with their own private key (satisfying `validateSignature()`, which only checks `contract(0)`).
3. Submit the transaction. `ActuatorCreator.createActuator()` creates actuators for both contracts; the second actuator executes state changes against the victim's account, whose owner never authorized this specific contract.

Because I could not verify within the indexed codebase whether an earlier check blocks `getContractCount() > 1`, this finding should be validated by a Devin session with full repository/test access to confirm whether crafting and broadcasting such a multi-contract transaction is actually accepted end-to-end (via `Manager.pushTransaction` → `TransactionCapsule.validatePubSignature` → `ActuatorCreator`).

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

**File:** chainbase/src/main/java/org/tron/core/capsule/TransactionCapsule.java (L698-704)
```java
  public boolean validateSignature(AccountStore accountStore,
      DynamicPropertiesStore dynamicPropertiesStore) throws ValidateSignatureException {
    if (!isVerified) {
      //Do not support multi contracts in one transaction
      Transaction.Contract contract = this.getInstance().getRawData().getContract(0);
      if (contract.getType() != ContractType.ShieldedTransferContract) {
        validatePubSignature(accountStore, dynamicPropertiesStore);
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
