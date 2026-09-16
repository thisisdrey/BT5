## Title
Native `FreezeBalanceProcessor` (TVM freeze precompile) implicitly creates receiver accounts, bypassing the account-creation fee/authorization gate enforced by every other account-creation path - (File: `actuator/src/main/java/org/tron/core/vm/nativecontract/FreezeBalanceProcessor.java`)

### Summary
`FreezeBalanceProcessor.validate()`, the native-contract handler invoked from the TVM when a smart contract calls the freeze-and-delegate precompile, silently creates a brand-new on-chain account for an arbitrary `receiverAddress` if it does not already exist, with no fee charged and no equivalent authorization check that the analogous user-facing actuator enforces.

### Finding Description
Every "normal" path that can create a TRON account charges an explicit fee/bandwidth cost gate before doing so:
- `CreateAccountActuator.execute` deducts `calcFee()` from the caller and burns/moves it to the blackhole address. [1](#0-0) 
- `TransferActuator` and `TransferAssetActuator` add `dynamicStore.getCreateNewAccountFeeInSystemContract()` to the fee whenever the `toAddress` does not exist yet. [2](#0-1) [3](#0-2) 
- `BandwidthProcessor.contractCreateNewAccount` is the check used elsewhere to determine whether the extra bandwidth/fee for account creation must be billed; it is only wired up for `AccountCreateContract`, `TransferContract`, and `TransferAssetContract` — any other contract type falls through to `default: return false`, meaning no creation fee is billed for it. [4](#0-3) 
- The user-facing `DelegateResourceActuator.validate()` explicitly **forbids** delegating resources to a non-existent receiver, throwing `ContractValidateException` — it never creates an account implicitly. [5](#0-4) 

In contrast, the native TVM-only `FreezeBalanceProcessor.validate()` (the "old" freeze-and-delegate-resource precompile reachable from inside a smart contract) does the opposite: if the receiver does not exist it silently calls `repo.createNormalAccount(receiverAddress)` and proceeds, with `calcFee()`-style billing entirely absent from this code path (its fee-related processors always `return 0` for related actuators and `contractCreateNewAccount` does not recognize this contract type at all). [6](#0-5) 

This mirrors the SurrealDB analog exactly: an "implicit creation" side-effect of an otherwise-unrelated operation (`USE NS/DB` in SurrealDB, freeze-and-delegate in java-tron) bypasses the authorization/cost gate that the equivalent *explicit* creation path enforces. Here, the "authorization" is the mandatory account-creation fee — any contract-controlled call into this native contract can spawn new, permanently-stored `AccountCapsule` entries for arbitrary addresses at zero cost, something neither `TransferActuator`, `TransferAssetActuator`, nor `DelegateResourceActuator` allow without payment (or at all, in `DelegateResourceActuator`'s case).

### Impact Explanation
Any account able to deploy or call a smart contract (a permissionless, unprivileged action) can repeatedly trigger the native freeze-delegate precompile with arbitrary never-before-seen `receiverAddress` values, forcing the node to persist new `AccountCapsule` records in `AccountStore` without paying the `CreateNewAccountFee`/bandwidth cost that the protocol otherwise mandates for account creation. This is an unbacked, fee-bypassing account-creation primitive that can be used to inflate the account database at will, undermining the economic assumption (paid account creation) that the rest of the protocol relies on to rate-limit state growth — a resource/DoS-adjacent impact directly analogous to the SurrealDB catalog-exhaustion scenario, but reached here through an unprivileged, single-transaction TVM call rather than admin/DEFINE authority.

### Likelihood Explanation
High. The path is reachable by any address that can send a `TriggerSmartContract` transaction to a deployed contract exercising the native freeze-delegate opcode — no special permission, staking, or witness/committee role is required. The precompile is directly reachable within `actuator/src/main/java/org/tron/core/vm/nativecontract/FreezeBalanceProcessor.java`, invoked from the VM `Program` execution flow.

### Recommendation
Align `FreezeBalanceProcessor.validate()` with the rest of the codebase: either (a) require the receiver account to already exist, matching `DelegateResourceActuator`'s behavior, or (b) if implicit creation must be retained for backward compatibility, charge the same `CreateNewAccountFee`/bandwidth cost that `TransferActuator`/`TransferAssetActuator` charge, and register this contract type in `BandwidthProcessor.contractCreateNewAccount` so the fee is actually billed before `execute()` runs.

### Proof of Concept
Not executable in this ask-only review; the note below explains why.

**Uncertainty / what I could not fully verify:** I was unable to trace the complete call chain from `Program.java` into `FreezeBalanceProcessor`/`FreezeBalanceV2Processor` (the grep for these symbols in `Program.java` returned no matches even though `Program.java` is reported elsewhere to reference `FreezeBalanceProcessor` 10 times), nor confirm whether `FreezeBalanceV2Processor` (the newer variant) has the same implicit-creation-without-fee behavior, or whether any TVM-level energy cost indirectly compensates for the missing bandwidth/fee charge. A full trace of the opcode/precompile dispatch in `Program.java` and `FreezeBalanceV2Processor.java`, plus a live transaction PoC, would need to be done in a Devin session with repo access to conclusively confirm exploitability and quantify the exact bypassed cost.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/CreateAccountActuator.java (L37-56)
```java
    long fee = calcFee();
    DynamicPropertiesStore dynamicStore = chainBaseManager.getDynamicPropertiesStore();
    AccountStore accountStore = chainBaseManager.getAccountStore();
    try {
      AccountCreateContract accountCreateContract = any.unpack(AccountCreateContract.class);
      boolean withDefaultPermission =
          dynamicStore.getAllowMultiSign() == 1;
      AccountCapsule accountCapsule = new AccountCapsule(accountCreateContract,
          dynamicStore.getLatestBlockHeaderTimestamp(), withDefaultPermission, dynamicStore);

      accountStore
          .put(accountCreateContract.getAccountAddress().toByteArray(), accountCapsule);

      adjustBalance(accountStore, accountCreateContract.getOwnerAddress().toByteArray(), -fee);
      // Add to blackhole address
      if (dynamicStore.supportBlackHoleOptimization()) {
        dynamicStore.burnTrx(fee);
      } else {
        adjustBalance(accountStore, accountStore.getBlackhole(), fee);
      }
```

**File:** actuator/src/main/java/org/tron/core/actuator/TransferActuator.java (L48-58)
```java
      // if account with to_address does not exist, create it first.
      AccountCapsule toAccount = accountStore.get(toAddress);
      if (toAccount == null) {
        boolean withDefaultPermission =
            dynamicStore.getAllowMultiSign() == 1;
        toAccount = new AccountCapsule(ByteString.copyFrom(toAddress), AccountType.Normal,
            dynamicStore.getLatestBlockHeaderTimestamp(), withDefaultPermission, dynamicStore);
        accountStore.put(toAddress, toAccount);

        fee = fee + dynamicStore.getCreateNewAccountFeeInSystemContract();
      }
```

**File:** actuator/src/main/java/org/tron/core/actuator/TransferAssetActuator.java (L61-71)
```java
      byte[] toAddress = transferAssetContract.getToAddress().toByteArray();
      AccountCapsule toAccountCapsule = accountStore.get(toAddress);
      if (toAccountCapsule == null) {
        boolean withDefaultPermission =
            dynamicStore.getAllowMultiSign() == 1;
        toAccountCapsule = new AccountCapsule(ByteString.copyFrom(toAddress), AccountType.Normal,
            dynamicStore.getLatestBlockHeaderTimestamp(), withDefaultPermission, dynamicStore);
        accountStore.put(toAddress, toAccountCapsule);

        fee = fee + dynamicStore.getCreateNewAccountFeeInSystemContract();
      }
```

**File:** chainbase/src/main/java/org/tron/core/db/BandwidthProcessor.java (L259-286)
```java
  public boolean contractCreateNewAccount(Contract contract) {
    AccountCapsule toAccount;
    switch (contract.getType()) {
      case AccountCreateContract:
        return true;
      case TransferContract:
        TransferContract transferContract;
        try {
          transferContract = contract.getParameter().unpack(TransferContract.class);
        } catch (Exception ex) {
          throw new RuntimeException(ex.getMessage());
        }
        toAccount =
            chainBaseManager.getAccountStore().get(transferContract.getToAddress().toByteArray());
        return toAccount == null;
      case TransferAssetContract:
        TransferAssetContract transferAssetContract;
        try {
          transferAssetContract = contract.getParameter().unpack(TransferAssetContract.class);
        } catch (Exception ex) {
          throw new RuntimeException(ex.getMessage());
        }
        toAccount = chainBaseManager.getAccountStore()
            .get(transferAssetContract.getToAddress().toByteArray());
        return toAccount == null;
      default:
        return false;
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/DelegateResourceActuator.java (L203-209)
```java
    AccountCapsule receiverCapsule = accountStore.get(receiverAddress);
    if (receiverCapsule == null) {
      String readableOwnerAddress = StringUtil.createReadableString(receiverAddress);
      throw new ContractValidateException(
          ActuatorConstant.ACCOUNT_EXCEPTION_STR
              + readableOwnerAddress + NOT_EXIST_STR);
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/FreezeBalanceProcessor.java (L54-70)
```java
    // validate for delegating resource
    byte[] receiverAddress = param.getReceiverAddress();
    if (!FastByteComparisons.isEqual(ownerAddress, receiverAddress)) {
      param.setDelegating(true);

      // check if receiver account exists. if not, then create a new account
      AccountCapsule receiverCapsule = repo.getAccount(receiverAddress);
      if (receiverCapsule == null) {
        receiverCapsule = repo.createNormalAccount(receiverAddress);
      }

      // forbid delegating resource to contract account
      if (receiverCapsule.getType() == Protocol.AccountType.Contract) {
        throw new ContractValidateException(
            "Do not allow delegate resources to contract addresses");
      }
    }
```
