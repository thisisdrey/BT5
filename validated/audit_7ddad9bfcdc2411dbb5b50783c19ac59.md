## Title
Owner Can Set Arbitrary `AccountType` (Including `Contract`) for a New Account via `AccountCreateContract` Without Any Deployment - (File: `actuator/src/main/java/org/tron/core/actuator/CreateAccountActuator.java`)

### Summary
`CreateAccountActuator` lets any funded account create a brand-new account and freely set that new account's `AccountType` (`Normal`, `AssetIssue`, or `Contract`) via the `type` field of `AccountCreateContract`, with zero validation on the value. This mirrors the Linea `setCustomContract` bug class: a caller can assign a protocol-meaningful status/type flag to an entity without the invariants that normally accompany that status (e.g., an actual deployed contract, or an actual asset issuance), bypassing the protocol paths (`CreateSmartContract`/`AssetIssueContract`) that are supposed to be the only ways to reach those states.

### Finding Description
`AccountCreateContract` carries an `AccountType type` field [1](#0-0) . `CreateAccountActuator.execute()` builds the new `AccountCapsule` directly from the caller-supplied contract, copying `contract.getType()`/`getTypeValue()` verbatim into the new account with no check against the type value: [2](#0-1) 

`CreateAccountActuator.validate()` checks the owner's balance/address and that `accountAddress` does not already exist, but explicitly has the type check commented out (`// if (contract.getType() == null) ...`), meaning **any** `AccountType` value, including `Contract`, is accepted without any evidence that the target address hosts an actual `SmartContract`/bytecode entry in `ContractStore`: [3](#0-2) 

By contrast, the legitimate way an account is marked `AccountType.Contract` is through actual contract creation (`CREATE`/`CREATE2` inside the VM), which always pairs the type change with writing a `SmartContract` capsule to `ContractStore` and clearing delegated resources: [4](#0-3) 

This is exactly the Linea `setCustomContract` bug shape: a caller-facing, transaction-broadcastable actuator writes a "status"/"type" field for a target entity without validating that the entity actually satisfies the invariant that field is supposed to represent (an asset for `NATIVE_STATUS`/`DEPLOYED_STATUS` in Linea; an actually-deployed contract for `AccountType.Contract` here).

### Impact Explanation
Any code path in java-tron or downstream tooling/exchanges that trusts `Account.type == Contract` as a proxy for "this address has deployed bytecode" can be fooled: a plain externally-owned address can be stamped as `Contract` without any code, `ContractCapsule`, or `CodeCapsule` entry. Depending on what consumers key off `AccountType.Contract` (wallets, exchanges, other services, or any future/committee-controlled logic gated on account type), this can be used to impersonate a contract account, potentially bypassing checks that differentiate EOA vs. contract behavior (e.g. resource/consumption assumptions, or off-chain risk/compliance logic relying on `wallet.getAccount().getType()`). It does not directly forge a working contract (there is no code at that address so calls will simply be no-ops), but it corrupts the account-type invariant that other subsystems (and third-party integrators using `TronJsonRpcImpl`/`Wallet` account queries) rely on being set only via the legitimate contract-creation path.

### Likelihood Explanation
Trivial and fully unprivileged: any account with sufficient TRX balance for the create-account fee can submit a normal signed `AccountCreateContract` transaction and choose an arbitrary `type` for a not-yet-existing address. No committee/witness/owner permission is required — this is reachable from a single broadcast transaction, matching the "single signed transaction" reachability bar.

### Recommendation
In `CreateAccountActuator.validate()`, restrict the accepted `AccountType` to `Normal` (and, if intentionally still supported, `AssetIssue` only after confirming asset-issue side effects are consistent), and reject `Contract` outright — contract accounts should only ever be created through the actual contract-deployment path (`CreateSmartContract`) that also writes the `ContractCapsule`/`CodeCapsule`. Re-enable and strengthen the commented-out type check that was disabled.

### Proof of Concept
1. Attacker account `A` (any funded, unprivileged address) picks an unused address `B`.
2. Attacker broadcasts a transaction with contract `AccountCreateContract{ owner_address = A, account_address = B, type = Contract }`.
3. `CreateAccountActuator.validate()` passes (only checks balance, address validity, non-existence) [5](#0-4) .
4. `CreateAccountActuator.execute()` creates account `B` with `Account.type = Contract` in `AccountStore`, while `ContractStore`/`CodeStore` have no entry for `B` [2](#0-1) .
5. Any query (`wallet.getAccount(B)`, JSON-RPC `eth_getCode`/account-type inspection tooling) now reports `B` as a `Contract`-type account, despite it having no deployed bytecode — an invariant break that only the internal VM `createContractImpl` path is supposed to establish together with a `ContractCapsule` write.

Note: I was unable to fully trace every downstream consumer of `Account.getType() == Contract` (some usages appear in `Wallet.java`, `Args.java`, `ConsensusService.java`, `Manager.java`) within the available exploration budget, so the exact severity of exploiting this mismatch elsewhere in the codebase could not be fully confirmed — a deeper review of those consumers is recommended to determine whether they assume the type/deployment invariant that this bug breaks.

### Citations

**File:** protocol/src/main/protos/core/contract/account_contract.proto (L26-30)
```text
message AccountCreateContract {
  bytes owner_address = 1;
  bytes account_address = 2;
  AccountType type = 3;
}
```

**File:** actuator/src/main/java/org/tron/core/actuator/CreateAccountActuator.java (L40-48)
```java
    try {
      AccountCreateContract accountCreateContract = any.unpack(AccountCreateContract.class);
      boolean withDefaultPermission =
          dynamicStore.getAllowMultiSign() == 1;
      AccountCapsule accountCapsule = new AccountCapsule(accountCreateContract,
          dynamicStore.getLatestBlockHeaderTimestamp(), withDefaultPermission, dynamicStore);

      accountStore
          .put(accountCreateContract.getAccountAddress().toByteArray(), accountCapsule);
```

**File:** actuator/src/main/java/org/tron/core/actuator/CreateAccountActuator.java (L67-124)
```java
  @Override
  public boolean validate() throws ContractValidateException {
    if (this.any == null) {
      throw new ContractValidateException(ActuatorConstant.CONTRACT_NOT_EXIST);
    }
    if (chainBaseManager == null) {
      throw new ContractValidateException("No account store or contract store!");
    }
    AccountStore accountStore = chainBaseManager.getAccountStore();
    if (!any.is(AccountCreateContract.class)) {
      throw new ContractValidateException(
          "contract type error,expected type [AccountCreateContract],real type[" + any
              .getClass() + "]");
    }
    final AccountCreateContract contract;
    try {
      contract = this.any.unpack(AccountCreateContract.class);
    } catch (InvalidProtocolBufferException e) {
      logger.debug(e.getMessage(), e);
      throw new ContractValidateException(e.getMessage());
    }
//    if (contract.getAccountName().isEmpty()) {
//      throw new ContractValidateException("AccountName is null");
//    }
    byte[] ownerAddress = contract.getOwnerAddress().toByteArray();
    if (!DecodeUtil.addressValid(ownerAddress)) {
      throw new ContractValidateException("Invalid ownerAddress");
    }

    AccountCapsule accountCapsule = accountStore.get(ownerAddress);
    if (accountCapsule == null) {
      String readableOwnerAddress = StringUtil.createReadableString(ownerAddress);
      throw new ContractValidateException(
          ActuatorConstant.ACCOUNT_EXCEPTION_STR
              + readableOwnerAddress + NOT_EXIST_STR);
    }

    final long fee = calcFee();
    if (accountCapsule.getBalance() < fee) {
      throw new ContractValidateException(
          "Validate CreateAccountActuator error, insufficient fee.");
    }

    byte[] accountAddress = contract.getAccountAddress().toByteArray();
    if (!DecodeUtil.addressValid(accountAddress)) {
      throw new ContractValidateException("Invalid account address");
    }

//    if (contract.getType() == null) {
//      throw new ContractValidateException("Type is null");
//    }

    if (accountStore.has(accountAddress)) {
      throw new ContractValidateException("Account has existed");
    }

    return true;
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L836-852)
```java
    AccountCapsule existingAccount = getContractState().getAccount(newAddress);
    boolean contractAlreadyExists = existingAccount != null;

    if (VMConfig.allowTvmConstantinople()) {
      contractAlreadyExists =
          contractAlreadyExists && isContractExist(existingAccount, getContractState());
    }
    Repository deposit = getContractState().newRepositoryChild();
    if (VMConfig.allowTvmConstantinople()) {
      if (existingAccount == null) {
        deposit.createAccount(newAddress, "CreatedByContract",
            AccountType.Contract);
      } else if (!contractAlreadyExists) {
        existingAccount.updateAccountType(AccountType.Contract);
        existingAccount.clearDelegatedResource();
        deposit.updateAccount(newAddress, existingAccount);
      }
```
