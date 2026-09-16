Confirmed: `TransferActuator.execute()` auto-creates a `Normal` account at any `toAddress` that doesn't yet exist [1](#0-0) , and `CreateAccountActuator.validate()` unconditionally rejects the transaction if that address already exists [2](#0-1) . This gives a concrete java-tron analog of the report's front-running griefing pattern.

### Title
Adversary can permanently block a targeted `AccountCreateContract` by front-running with a trivial TRX transfer to the target address - (File: `actuator/src/main/java/org/tron/core/actuator/CreateAccountActuator.java`)

### Summary
`CreateAccountActuator` is the java-tron analog of the reported `IVotingStrategy.init()` pattern: it performs a one-time, irreversible "claim" of an address (`accountStore.has(accountAddress)` check) that any unrelated, unprivileged account can pre-empt for the target address, permanently blocking the legitimate transaction from ever succeeding.

### Finding Description
`CreateAccountActuator.validate()` requires that no account currently exists at the `accountAddress` specified in the `AccountCreateContract`, throwing `"Account has existed"` otherwise [3](#0-2) . `execute()` then creates the `AccountCapsule` for that address using the fields of the submitted contract (including `withDefaultPermission` multisig setup) [4](#0-3) .

Separately, `TransferActuator.execute()` silently auto-creates a plain `Normal`-type account at any `toAddress` the very first time TRX is sent to it, with no signature or ownership check on that address required [1](#0-0) . Any address on the network is a valid `toAddress` for a `TransferContract`.

Because `AccountCreateContract` addresses (e.g., a deposit address an exchange or dApp is pre-provisioning for a user, before that user has ever transacted) are typically known or predictable ahead of the intended creation transaction being broadcast/mined, an adversary observing the mempool (or simply guessing/monitoring a known deterministic address scheme) can front-run the legitimate `AccountCreateContract` with a minimal `TransferContract` (e.g., 1 sun) to the same `accountAddress`. This auto-creates a bare `Normal` account there via `TransferActuator`, and the subsequent legitimate `AccountCreateContract` for that same address will permanently fail validation with `"Account has existed"` — there is no mechanism to convert an existing account back into one created "properly" via `AccountCreateContract`, so the intended account configuration (e.g. multisig permission bootstrap) can never be established for that address going forward.

This mirrors the reported bug class exactly: a permissionless, one-time "claim" operation (`init()` / `accountStore.has(...)` guard) on a resource whose target identifier is known in advance can be front-run by any unrelated party at negligible cost, permanently denying the intended operation for that resource.

### Impact Explanation
This is a griefing/DOS vector against any protocol or exchange workflow that relies on `AccountCreateContract` to provision accounts with specific initial configuration (e.g., default multisig permission structure via `withDefaultPermission`) at addresses that become known before the creation transaction lands on-chain. The attack costs the adversary only the `TRANSFER_FEE` plus 1 sun, and permanently and irreversibly prevents the legitimate account bootstrap for that specific address — the address can never subsequently be "created" via `AccountCreateContract`. This qualifies as Medium severity: it does not directly move funds, but it permanently denies an intended account state configuration and forces the affected party to use a different address, similarly to how the original report describes permanent DOS of round creation.

### Likelihood Explanation
Likelihood is contingent on the attacker knowing or predicting the target `accountAddress` before the `AccountCreateContract` transaction is confirmed (e.g., by observing the pending transaction in the mempool, or via off-chain address pre-communication in an exchange/custodial deposit-address workflow). Given TRON's public mempool and the low cost of the griefing transaction (a single `TransferContract` for 1 sun), this is easily executable by any funded account once the target address is known.

### Recommendation
Do not treat address "already exists as a plain `Normal` account (created implicitly via TRX transfer)" the same as "already properly created". Consider allowing `AccountCreateContract` to upgrade/overwrite a pre-existing implicitly-created `Normal` account that has never been the target of an explicit `AccountCreateContract` or `AccountPermissionUpdateContract` (analogous to how `Program.createContractImpl`/CREATE2 already distinguishes and upgrades pre-existing plain accounts at a predicted address rather than failing outright) [5](#0-4) , or document that `AccountCreateContract` should not be relied upon for addresses that are shared/communicated off-chain before submission.

### Proof of Concept
1. Alice (e.g., an exchange) plans to bootstrap a multisig deposit account for Bob at address `X` and broadcasts an `AccountCreateContract` transaction with `accountAddress = X`.
2. Attacker observes this transaction in the mempool (or independently learns `X` ahead of time) and broadcasts a `TransferContract` sending `1` sun from any funded account to `toAddress = X`, with a higher fee/priority so it lands first.
3. `TransferActuator.execute()` runs first and creates a bare `Normal` `AccountCapsule` at `X` [6](#0-5) .
4. Alice's `AccountCreateContract` transaction is then processed; `CreateAccountActuator.validate()` finds `accountStore.has(X) == true` and throws `"Account has existed"`, permanently rejecting the transaction [2](#0-1) .
5. Address `X` can never subsequently be provisioned via `AccountCreateContract`; Alice must pick a new address for Bob's account.

### Citations

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

**File:** actuator/src/main/java/org/tron/core/actuator/CreateAccountActuator.java (L110-121)
```java
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
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L844-852)
```java
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
