Based on the investigation, I found a concrete analog in java-tron's transaction signature verification, which mirrors the CVE's root cause of "not correlating identity across multiple items within a single message."

### Title
Transaction signature/permission verification only binds to the first contract's owner, not all contracts in a multi-contract transaction - (File: chainbase/src/main/java/org/tron/core/capsule/TransactionCapsule.java)

### Summary
CVE-2025-66270 describes a class of bug where a protocol accepts a multi-packet exchange without correlating an identity established in one packet with the identity used in a later packet, allowing a party to substitute a different identity mid-exchange. The java-tron transaction validation path exhibits the same class of bug: `TransactionCapsule.validateSignature()` explicitly verifies the signature/permission binding only for `contract(0)` of a transaction's contract list, while downstream execution can walk the full `getContractList()`. The owner-account binding used by each actuator's `validate()`/`execute()` is read directly from that specific sub-contract's `ownerAddress` field rather than being re-correlated against the transaction-level signer that was actually cryptographically verified.

### Finding Description
`TransactionCapsule.validateSignature()` contains an explicit design note that multi-contract transactions are not supported, and only checks the first contract: [1](#0-0) 

This delegates to `validatePubSignature()` / `validateSignature(Transaction, ...)`, which likewise pulls only `contract.getRawData().getContract(0)`'s owner address and permission to check against the supplied signatures: [2](#0-1) 

Meanwhile, each actuator resolves its own "owner" identity independently from the specific `Contract`/`Any` payload it was constructed from — e.g. `TransferActuator.validate()` reads `ownerAddress` straight out of the unpacked `TransferContract`, with no reference back to whichever address was cryptographically verified against the transaction's signature list: [3](#0-2) 

`Manager.java` was confirmed to reference `getContractList()`/`getContractCount()` (multiple matches), indicating contract-list iteration exists in the block-application/execution path rather than being hard-limited to a single contract, though I was not able to fully trace whether current production logic strictly rejects `getContractCount() > 1` before an actuator is built for each contract — no such guard (e.g. `"only one contract"`, `getContractCount() != 1`) was found anywhere in the indexed codebase via targeted search.

If a transaction with multiple `Contract` entries is accepted, and each entry's actuator independently trusts its own embedded `ownerAddress` for authorization while only `contract(0)`'s signature/permission is ever validated, then contract entries at index ≥ 1 are executed under an owner identity that was never bound to (correlated with) a verified signature — the exact "device ID / identity not correlated across two packets" bug class from the CVE, transplanted to "owner identity not correlated across two contracts in one transaction."

### Impact Explanation
If reachable (i.e., if any code path accepts and executes a transaction with more than one `Contract` and no upstream guard rejects it), this would let an attacker craft one transaction that is validly signed only for its first sub-contract, but that also contains a second sub-contract naming an arbitrary victim account as `ownerAddress`, causing unauthorized account operations (e.g., unauthorized transfers, asset operations, or permission changes) against accounts that never signed the transaction — an unauthorized account operation / theft-of-funds impact.

### Likelihood Explanation
I could not confirm from available context whether an upstream guard (transaction pool validation, `TransactionUtil`, or protobuf-level constraints) unconditionally rejects transactions with `getContractCount() > 1` before actuators are constructed. My searches for an explicit single-contract enforcement (e.g., `"only one contract"`, `getContractCount() != 1`) returned no matches in the indexed codebase, but this does not conclusively prove no such guard exists elsewhere (e.g., in code not covered by the index, or in `ActuatorCreator`/`TransactionUtil.validContractProto`, which I could not fully inspect within the remaining budget). Given this uncertainty, I cannot assert with confidence that this path is reachable from an unprivileged broadcaster today.

### Recommendation
Explicitly enforce and audit that (a) every transaction is limited to exactly one `Contract` before any actuator is constructed, and (b) `validateSignature`/`checkWeight` is applied per-contract (correlating each contract's declared owner with a specific verified signature/permission) rather than only against `contract(0)`, closing any latent gap between the verified identity and the identity actually used for execution.

### Proof of Concept
Not constructible with certainty from the available index: reproducing this requires confirming there is no pre-existing single-contract enforcement between transaction ingestion and actuator execution, which I could not fully verify. A concrete PoC would be: build a `Transaction` with `rawData.contract[0]` = a validly-signed, attacker-owned `TransferContract`, and `rawData.contract[1]` = a `TransferContract`/`AccountPermissionUpdateContract` whose `ownerAddress` is set to a victim account, then submit via broadcast; if `Manager` executes both contracts while `validateSignature` only checked `contract[0]`, contract[1] would execute unauthorized. [1](#0-0) [2](#0-1) [3](#0-2)

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

**File:** actuator/src/main/java/org/tron/core/actuator/TransferActuator.java (L100-119)
```java
    byte[] toAddress = transferContract.getToAddress().toByteArray();
    byte[] ownerAddress = transferContract.getOwnerAddress().toByteArray();
    long amount = transferContract.getAmount();

    if (!DecodeUtil.addressValid(ownerAddress)) {
      throw new ContractValidateException("Invalid ownerAddress!");
    }
    if (!DecodeUtil.addressValid(toAddress)) {
      throw new ContractValidateException("Invalid toAddress!");
    }

    if (Arrays.equals(toAddress, ownerAddress)) {
      throw new ContractValidateException("Cannot transfer TRX to yourself.");
    }

    AccountCapsule ownerAccount = accountStore.get(ownerAddress);

    if (ownerAccount == null) {
      throw new ContractValidateException("Validate TransferContract error, no OwnerAccount.");
    }
```
