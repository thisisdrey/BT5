### Title
Front-running griefing permanently blocks CreateSmartContract (deployment) transaction with a pre-funded address collision - ([File: actuator/src/main/java/org/tron/core/actuator/VMActuator.java])

### Summary
`generateContractAddress` derives the new contract's address deterministically from the transaction id (raw-data hash) and the deployer's owner address [1](#0-0) . Once a signed `CreateSmartContract` transaction is visible (e.g. in the mempool/relayed before inclusion), any unprivileged account can compute this exact target address and send a plain TRX transfer to it, creating a `Normal` account there before the deployment transaction executes. When `VMActuator.create()` later runs, it unconditionally rejects deployment if any account already exists at that address, with no distinction between a "real" contract and a plain funded EOA, and no way to reclaim/clear the address: [2](#0-1) .

### Finding Description
This mirrors the report's bug class: a lower-privileged/unrelated on-chain actor can pre-occupy an ID/address slot that a normal, sequential/deterministic-creation code path assumes is free, and the normal path has no recovery mechanism (analogous to `mint()` having no `burn()`), permanently reverting the legitimate operation for that transaction.

In java-tron:
1. `WalletUtil.generateContractAddress(trx)` computes the deployment address purely from `sha3omit12(txId || ownerAddress)` [1](#0-0)  — fully predictable by anyone who observes the signed transaction before it is confirmed.
2. `VMActuator.create()` validates uniqueness of that address by checking `rootRepository.getAccount(contractAddress) != null` and throws `ContractValidateException("Trying to create a contract with existing contract address...")` if any account (of any type, including a plain `Normal` account created by an ordinary TRX transfer) already occupies the slot [2](#0-1) .
3. Unlike the internal `CREATE2` path in `Program.createContractImpl`, which specifically distinguishes a "real" existing contract from a merely-funded account and allows deployment to upgrade a funded EOA in place (`existingAccount.updateAccountType(AccountType.Contract)`) when `allowTvmConstantinople()` is enabled [3](#0-2) , the top-level `VMActuator.create()` path used for ordinary `CreateSmartContract` transactions has no such exemption — it always fails hard on any pre-existing account, contract or not.
4. TRON accounts, once created (even by a 0-value/minimal-value transfer), cannot be deleted, so an attacker's front-run transfer permanently poisons that specific derived address for that exact transaction.

### Impact Explanation
A malicious mempool observer can grief a targeted contract deployment: by sending a cheap TRX transfer to the deterministic target address before the deployer's `CreateSmartContract` transaction is included, the attacker forces that specific signed transaction to fail forever with "Trying to create a contract with existing contract address," wasting the deployer's fee/energy and blocking that deployment attempt. This is a targeted denial-of-service against contract deployment reachable by any unprivileged account broadcasting an ordinary transfer transaction, with no built-in recovery for that transaction (the account cannot be removed).

### Likelihood Explanation
Requires the attacker to observe a pending `CreateSmartContract` transaction (mempool visibility) and race a transfer transaction to land first — feasible on a live network with public mempools/relaying, though it only blocks that specific signed transaction (not the deployer's ability to build and sign a new transaction with a different raw-data hash/timestamp, which yields a different address). This limits it to a repeatable griefing/DoS vector rather than an absolute permanent block on the deployer ever deploying a contract from that address, unless the attacker can consistently front-run every retry.

### Recommendation
Align the top-level `CreateSmartContract` path with the `CREATE2` handling in `Program.createContractImpl`: when an account already exists at the computed address, check whether it is a genuine contract (has code/contract metadata) versus a plain funded account, and if it is not a real contract, allow deployment to proceed by upgrading the account type in place rather than unconditionally rejecting the transaction.

### Proof of Concept
1. Attacker monitors the mempool and observes a pending `CreateSmartContract` transaction `T` from address `A`.
2. Attacker computes `contractAddress = sha3omit12(txId(T) || A)` using the same formula as `WalletUtil.generateContractAddress` [1](#0-0) .
3. Attacker broadcasts a `TransferContract` sending a minimal amount of TRX to `contractAddress`, which gets included first, creating a `Normal` account there.
4. When `T` is processed, `VMActuator.create()` hits `rootRepository.getAccount(contractAddress) != null` and throws `ContractValidateException`, permanently failing that specific transaction [2](#0-1) .

### Citations

**File:** chainbase/src/main/java/org/tron/common/utils/WalletUtil.java (L39-52)
```java
  public static byte[] generateContractAddress(Transaction trx) {

    CreateSmartContract contract = ContractCapsule.getSmartContractFromTransaction(trx);
    byte[] ownerAddress = contract.getOwnerAddress().toByteArray();
    TransactionCapsule trxCap = new TransactionCapsule(trx);
    byte[] txRawDataHash = trxCap.getTransactionId().getBytes();

    byte[] combined = new byte[txRawDataHash.length + ownerAddress.length];
    System.arraycopy(txRawDataHash, 0, combined, 0, txRawDataHash.length);
    System.arraycopy(ownerAddress, 0, combined, txRawDataHash.length, ownerAddress.length);

    return Hash.sha3omit12(combined);

  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/VMActuator.java (L358-364)
```java
    byte[] contractAddress = WalletUtil.generateContractAddress(trx);
    // insure the new contract address haven't exist
    if (rootRepository.getAccount(contractAddress) != null) {
      throw new ContractValidateException(
          "Trying to create a contract with existing contract address: " + StringUtil
              .encode58Check(contractAddress));
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
