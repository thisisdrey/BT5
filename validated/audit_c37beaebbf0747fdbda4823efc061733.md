### Title
NullPointerException in `TransactionTrace.checkIsConstant()` from unchecked null return of `ContractCapsule.getTriggerContractFromTransaction()` - (File: chainbase/src/main/java/org/tron/core/db/TransactionTrace.java)

### Summary
`ContractCapsule.getTriggerContractFromTransaction()` swallows `InvalidProtocolBufferException` and returns `null` when the transaction's `Any` contract parameter fails to unpack as `TriggerSmartContract`. `TransactionTrace.checkIsConstant()` calls this method and immediately dereferences the result without a null check, exactly mirroring the ksmbd bug class: a function that can legitimately return `null` on failure is used by a caller that assumes success and dereferences the pointer/reference unconditionally.

### Finding Description
`getTriggerContractFromTransaction` returns `null` on any unpack failure: [1](#0-0) 

`checkIsConstant()` consumes that result and, for a `TRX_CONTRACT_CALL_TYPE` transaction, immediately calls `.getContractAddress()` on it with no null check: [2](#0-1) 

A `Transaction.Contract` protobuf message carries the `type` enum (`ContractType.TriggerSmartContract`) and the `parameter` (`Any`) as independently settable fields. An attacker fully controlling the raw transaction bytes for their own signed transaction can set `type = TriggerSmartContract` while making `parameter` fail to unpack into a `TriggerSmartContract` message (e.g., a mismatched/corrupted `Any` payload), causing `InvalidProtocolBufferException` to be caught and `null` returned. `checkIsConstant()` then throws an unhandled `NullPointerException` while dereferencing `triggerContractFromTransaction.getContractAddress()`.

This function is invoked from `Manager` during transaction processing (confirmed by two call sites in `framework/src/main/java/org/tron/core/db/Manager.java`, though I was not able to inspect their exact surrounding call stack before running out of tool iterations — this should be verified in a follow-up).

### Impact Explanation
An uncaught `NullPointerException` thrown while processing a transaction inside block/transaction application logic can propagate and abort processing on every node validating that transaction/block, since it is not one of the caught, expected validation exceptions (`ContractValidateException`, `VMIllegalException`). This is a low-cost, unauthenticated denial-of-service vector: a single malformed but validly-signed `TriggerSmartContract`-typed transaction can crash or halt node transaction processing network-wide when propagated, potentially causing a chain halt — directly analogous to the "illegal memory write"/crash impact of the original ksmbd bug, adapted to Java's crash-on-NPE failure mode.

### Likelihood Explanation
Any unprivileged account holder can construct a transaction with an internally inconsistent `Contract` message (type vs. parameter mismatch) and sign it validly; no special privilege, contract deployment, or SR/witness role is required to reach this code path since `TransactionTrace` participates in the standard transaction execution pipeline shared by all `TriggerSmartContract` transactions.

### Recommendation
In `TransactionTrace.checkIsConstant()`, check `triggerContractFromTransaction == null` (or convert the failure into a caught `ContractValidateException`) before dereferencing `.getContractAddress()`, matching the null-check pattern already used elsewhere for the same accessor (e.g., in `VMActuator.call()`): [3](#0-2) 

### Proof of Concept
1. Construct a `Transaction` whose single `Contract` entry has `type = ContractType.TriggerSmartContract`.
2. Set `parameter` to an `Any` whose embedded bytes do not correctly parse as a `TriggerSmartContract` message (e.g., reuse another contract type's serialized bytes or truncate/corrupt the payload) so that `Any.unpack(TriggerSmartContract.class)` throws `InvalidProtocolBufferException`.
3. Sign the transaction normally with a valid account key and broadcast it.
4. When `TransactionTrace.checkIsConstant()` runs (`allowTvmConstantinople != 1`), `ContractCapsule.getTriggerContractFromTransaction()` returns `null`, and `triggerContractFromTransaction.getContractAddress()` throws `NullPointerException`, which is not one of the caught validation exception types — the note is uncertain without confirming the exact caller context in `Manager.java`, which should be validated directly in the repository.

### Citations

**File:** chainbase/src/main/java/org/tron/core/capsule/ContractCapsule.java (L65-73)
```java
  public static TriggerSmartContract getTriggerContractFromTransaction(Transaction trx) {
    try {
      Any any = trx.getRawData().getContract(0).getParameter();
      TriggerSmartContract contractTriggerContract = any.unpack(TriggerSmartContract.class);
      return contractTriggerContract;
    } catch (InvalidProtocolBufferException e) {
      return null;
    }
  }
```

**File:** chainbase/src/main/java/org/tron/core/db/TransactionTrace.java (L133-153)
```java
  public void checkIsConstant() throws ContractValidateException, VMIllegalException {
    if (dynamicPropertiesStore.getAllowTvmConstantinople() == 1) {
      return;
    }
    TriggerSmartContract triggerContractFromTransaction = ContractCapsule
        .getTriggerContractFromTransaction(this.getTrx().getInstance());
    if (TRX_CONTRACT_CALL_TYPE == this.trxType) {
      ContractCapsule contract = contractStore
          .get(triggerContractFromTransaction.getContractAddress().toByteArray());
      if (contract == null) {
        throw new ContractValidateException(String.format("contract: %s is not in contract store",
            StringUtil.encode58Check(triggerContractFromTransaction
                .getContractAddress().toByteArray())));

      }
      ABI abi = contract.getInstance().getAbi();
      if (WalletUtil.isConstant(abi, triggerContractFromTransaction)) {
        throw new VMIllegalException("cannot call constant method");
      }
    }
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/VMActuator.java (L489-492)
```java
    TriggerSmartContract contract = ContractCapsule.getTriggerContractFromTransaction(trx);
    if (contract == null) {
      return;
    }
```
