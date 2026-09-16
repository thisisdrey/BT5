### Title
Front-runnable contract-address collision blocks legitimate `CreateSmartContract` deployment - (File: `actuator/src/main/java/org/tron/core/actuator/VMActuator.java`)

### Summary
The top-level `CreateSmartContract` actuator computes the new contract's address deterministically from the pending transaction's raw-data hash and the owner address, and unconditionally rejects deployment if *any* account (even a plain, code-less account) already exists at that address. Because the raw-data hash of a transaction is visible to anyone observing the transaction before it is included in a block (mempool/relay), an attacker can pre-compute the exact contract address and race a cheap "create account" style transaction (or a plain transfer that auto-creates an account) to that address, permanently blocking the intended deployment for that specific signed transaction — the same class of "unauthorized account creation blocks legitimate deployment" issue described in the Evmos vesting-module advisory.

### Finding Description
In `VMActuator.create()`, the contract address is computed with `WalletUtil.generateContractAddress(trx)`: [1](#0-0) 

This hashes the transaction's own `txRawDataHash` together with the owner address — meaning the address is fully determined the moment the transaction is signed and becomes visible in the network, well before it is packed into a block.

`VMActuator.create()` then performs a strict existence check and rejects deployment if any account object already occupies that address: [2](#0-1) 

This is notably stricter than the internal `CREATE`/`CREATE2` path used for contract-to-contract creation (`Program.createContractImpl`), which explicitly tolerates and *upgrades* a pre-existing plain (non-contract) account in place, preserving its balance, rather than failing the deployment: [3](#0-2) 

This "upgrade in place" pattern is intentionally documented and tested elsewhere in the codebase for the CREATE2-collision case: [4](#0-3) [5](#0-4) 

But the top-level `CreateSmartContract` actuator does not follow this same tolerant pattern — it hard-fails validation instead of taking over the pre-existing account, exposing a front-running griefing vector: any unprivileged party that observes a broadcast `CreateSmartContract` transaction can compute `sha3(txRawDataHash || ownerAddress)` and race a normal-account-creating transaction (`AccountCreateContract`, handled by `CreateAccountActuator`, or a simple TRX transfer that implicitly creates the destination account) to that same address ahead of block inclusion.

### Impact Explanation
A successful race permanently prevents that specific signed `CreateSmartContract` transaction from succeeding — the deployer's transaction fails validation with "Trying to create a contract with existing contract address," wasting the deployer's fee/bandwidth and denying them the ability to deploy at that address for that transaction. Since the address is derived from the transaction's own hash (not a nonce), the deployer must resign and rebroadcast a new transaction to get a fresh address, and a persistent attacker monitoring the mempool can repeat the front-run each time, effectively denying deployment indefinitely — matching the "prevent smart contracts from being deployed correctly" impact called out in the referenced advisory. This is a griefing/DoS on contract deployment reachable by any unprivileged transaction broadcaster.

### Likelihood Explanation
Exploitation only requires observing a pending transaction (any node with mempool visibility, including public nodes or ordinary network participants) and computing a SHA3 hash — no special privileges, keys, or validator status are needed. The attacker's counter-transaction (a cheap account-creation or transfer) is low-cost and can be prioritized ahead of the target transaction through normal fee/latency competition, which is realistic on a live network with public mempool visibility.

### Recommendation
Align `VMActuator.create()`'s pre-existing-account handling with the internal CREATE/CREATE2 semantics already implemented in `Program.createContractImpl`: when an account already exists at the computed contract address but has no code/contract metadata associated with it (i.e., is not already an actual deployed contract), upgrade it in place to a contract account (preserving balance) instead of rejecting the transaction outright. Reserve the hard validation failure for the case where a real smart contract (with code) already occupies that address.

### Proof of Concept
1. Attacker runs a full node or otherwise monitors the P2P/mempool for pending `CreateSmartContract` transactions.
2. Upon seeing a pending transaction `T` with owner address `O`, attacker computes `contractAddress = sha3(txRawDataHash(T) || O)` exactly as `WalletUtil.generateContractAddress` does.
3. Attacker submits a low-fee `AccountCreateContract` (or a plain TRX transfer) transaction targeting `contractAddress`, ensuring it lands in a block at or before `T`.
4. When `T` is processed, `VMActuator.create()`'s check `rootRepository.getAccount(contractAddress) != null` (VMActuator.java:360) is true, and the deployment fails with `ContractValidateException("Trying to create a contract with existing contract address: ...")`, even though no contract code exists at that address — denying the legitimate deployer.

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

**File:** framework/src/main/java/org/tron/core/db/HistoryBlockHashUtil.java (L80-92)
```java
  /**
   * Deploy the TIP-2935 BlockHashHistory contract at {@code HISTORY_STORAGE_ADDRESS}.
   * If foreign code or contract metadata already sits at the canonical address,
   * logs a warning and returns without writing — the collision is deterministic
   * across nodes (same pre-state ⇒ same decision), so the proposal flag still
   * commits and chain consensus is intact. The foreign contract executes as-is
   * on every node; TIP-2935 functionality is silently absent at this address.
   * A SHA-3 pre-image of the address is the only realistic way that branch
   * fires, so it's belt-and-braces. A pre-existing non-contract account at the
   * address is the common case (anyone can transfer TRX there to activate it
   * as an EOA), so we upgrade its type to {@code Contract} in place — matching
   * the CREATE2 collision branch ({@code updateAccountType} +
   * {@code clearDelegatedResource}) and preserving balance/asset state.
```

**File:** framework/src/test/java/org/tron/core/db/HistoryBlockHashIntegrationTest.java (L318-334)
```java
  @Test
  public void deployUpgradesPreExistingNormalAccountPreservingBalance() {
    byte[] addr = HistoryBlockHashUtil.HISTORY_STORAGE_ADDRESS;
    long balance = 12345L;
    AccountCapsule eoa = new AccountCapsule(
        ByteString.copyFrom(addr), Protocol.AccountType.Normal);
    eoa.setBalance(balance);
    chainBaseManager.getAccountStore().put(addr, eoa);

    HistoryBlockHashUtil.deploy(dbManager);

    AccountCapsule after = chainBaseManager.getAccountStore().get(addr);
    assertEquals(Protocol.AccountType.Contract, after.getType());
    assertEquals(balance, after.getBalance());
    assertTrue(chainBaseManager.getCodeStore().has(addr));
    assertTrue(chainBaseManager.getContractStore().has(addr));
  }
```
