### Title
Missing enforcement of single-contract transactions allows unauthorized actuation of unsigned contracts - (File: `chainbase/src/main/java/org/tron/core/capsule/TransactionCapsule.java`, `actuator/src/main/java/org/tron/core/actuator/ActuatorCreator.java`)

### Summary
The OpenVM report describes a class of bug where the verifier only inspects a fixed-index element (the 0th/1st/3rd AIR) of a list the prover fully controls, while all elements in that list still take effect, letting the prover forge state transitions for the elements the verifier never inspected. Java-tron has a structurally analogous pattern in transaction processing: `Transaction.raw` holds a `repeated Contract contract` list, but signature/permission verification is only ever performed against `contract(0)`, while the actuator-execution pipeline (`ActuatorCreator.createActuator`) iterates over and executes **every** contract in the list.

### Finding Description
`TransactionCapsule.validateSignature` explicitly derives ownership/permission only from the first contract: [1](#0-0) 

The comment `//Do not support multi contracts in one transaction` at line 701 documents the intended invariant, but nothing in this method — or in `validatePubSignature`, `validateSignature(Transaction, ...)`, or `checkWeight` — rejects a transaction whose `getRawData().getContractList()` has more than one element. Only `contract(0)`'s owner/permission is resolved and checked against the supplied signatures: [2](#0-1) 

Meanwhile, `ActuatorCreator.createActuator` builds and later executes an `Actuator` instance for **every** contract in `rawData.getContractList()`, with no reference to which contract's owner was actually authenticated by `validateSignature`: [3](#0-2) 

Each individual actuator (e.g. `TransferContract`, `TransferAssetContract`, `VoteWitnessContract`) validates its own fields (balances, existence, resource limits) but does not independently re-verify that the account named as `owner_address` in its specific contract was the one whose signature was checked — that binding is only established once, for index 0, in `validateSignature`. As a result, a transaction crafted with:
- `contract(0)`: any cheap contract type the attacker's own key legitimately owns/signs (satisfying `validateSignature`'s check against index 0), and
- `contract(1..n)`: contract(s) whose `owner_address` belongs to a victim account,

would pass `validateSignature` while still causing every actuator in the list — including the one impersonating the victim — to execute via `ActuatorCreator.createActuator` and the actuator execution loop in `Manager`.

This mirrors the OpenVM issue precisely: the verifier ("`validateSignature`") only checks the fixed 0th element of an attacker-controlled list, while the execution/prover side ("`ActuatorCreator`"/actuator execution) processes the full list and lets non-verified elements take effect.

### Impact Explanation
If confirmed reachable end-to-end, this would allow an unprivileged transaction broadcaster to perform unauthorized account operations (e.g. draining TRX/TRC10 balances, casting votes, or freezing/unfreezing resources) on behalf of any account, without that account's signature or permission — a direct theft-of-funds / unauthorized-account-operation scenario.

### Likelihood Explanation
Likelihood depends entirely on whether some other layer (block/transaction serialization limits, mempool admission, or a check I could not locate in the indexed code) enforces `getContractCount() == 1` before `validateSignature`/`ActuatorCreator` are reached. I found no such enforcement in `Manager.pushTransaction`, `Manager.validateCommon`, `Manager.validateDup`, or `ActuatorCreator`, all of which operate on the raw contract list without a count restriction. The presence of the explicit "Do not support multi contracts" comment, without a corresponding runtime check, is itself suspicious and suggests this invariant may not be actively enforced elsewhere.

### Recommendation
Add an explicit, hard check (ideally in `Manager.validateCommon` or `TransactionCapsule.validateSignature`) that rejects any transaction whose `rawData.getContractCount() != 1`, matching the documented single-contract design assumption, before the transaction is queued, validated, or handed to `ActuatorCreator`. Alternatively, if multi-contract transactions must be supported in the future, `validateSignature` must verify signatures/permissions against **every** contract's owner, not just index 0.

### Proof of Concept
Due to indexing limits I could not trace every downstream consumer (e.g. whether `TransactionUtil`/gRPC broadcast paths add an independent `contractCount == 1` guard before reaching `Manager.pushTransaction`). A concrete PoC would require constructing a `Transaction` with two `Contract` entries as described above, signing only for `contract(0)`'s owner, and submitting it via `Wallet.broadcastTransaction` to confirm whether `contract(1)` executes against the victim's account. I recommend starting a full Devin session with repository access to trace `Wallet.broadcastTransaction` → `Manager.pushTransaction` → `TransactionCapsule.validateSignature` → `ActuatorCreator.createActuator` → actuator execution end-to-end and confirm whether any additional gate on `getContractCount()` exists that I was unable to find in the indexed subset of the codebase.

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
