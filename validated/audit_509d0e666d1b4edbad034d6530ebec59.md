## Analysis

The reported bug class — an unprivileged party pre-funding an address to trip a rigid "state must be pristine" check and permanently DoS a legitimate operation — has a direct analog in java-tron's smart contract deployment path.

### Title
Contract deployment can be permanently griefed by front-running the deterministic contract address with a dust transfer — (File: `actuator/src/main/java/org/tron/core/actuator/VMActuator.java`)

### Summary
`VMActuator.create()` rejects a `CreateSmartContract` transaction outright if *any* account (even a plain, non-contract account created by a stray TRX transfer) already exists at the deterministically-computed contract address. Because that address is derivable from public transaction fields as soon as the deploy transaction is visible (e.g., in the mempool), any unprivileged broadcaster can front-run it with a trivial transfer to that address and force the deployer's transaction to fail every time it is (re)broadcast with that exact raw data, mirroring the WETHGateway griefing pattern where an unrelated dust deposit permanently breaks a legitimate operation.

### Finding Description
In `VMActuator.create()`, the contract address for a top-level `CREATE` is computed deterministically and then checked for prior existence: [1](#0-0) 

`WalletUtil.generateContractAddress(trx)` derives the address purely from the transaction's raw-data hash (txID) and the owner address: [2](#0-1) 

Both of these are known the moment the `CreateSmartContract` transaction is constructed/signed and are visible to any observer once the transaction enters the mempool (or even before broadcast, if the deployer's tooling/front-end exposes the unsigned raw data, e.g. via `Wallet`/`TronJsonRpcImpl` gas/energy estimation calls used before submission). An attacker who sees this pending transaction can immediately send a plain TRX `TransferContract` to the precomputed address. This causes `accountStore.put()`/`rootRepository.getAccount(contractAddress)` to return non-null for that address by the time the deploy transaction executes, so `create()` unconditionally throws:
```
"Trying to create a contract with existing contract address: ..."
```
This is unlike the internal `CREATE` opcode path (`Program.createContractImpl`), which tolerates a pre-funded/pre-existing plain account and still allows contract creation (it only fails when actual contract code/metadata is already present): [3](#0-2) 

The top-level actuator has no equivalent tolerance — any pre-existing account, regardless of type, trips the hard failure, exactly analogous to the WETHGateway's rigid `assert(_weth.balanceOf(address(this)) == amount)` being broken by an unrelated dust deposit.

### Impact Explanation
This is a availability/griefing issue rather than a fund-theft issue (consistent with the "High severity, not critical" characterization of the original report): a malicious, unprivileged actor observing the mempool can deterministically and repeatedly block a specific contract deployer's specific pending `CreateSmartContract` transaction from succeeding, wasting the deployer's fee/energy on a guaranteed-failing execution and degrading the deployment UX/availability for dApp deployers and any front-end tooling that relies on `CreateSmartContract`.

### Likelihood Explanation
Likelihood is moderate: it requires the attacker to observe a pending `CreateSmartContract` transaction (mempool visibility is realistic for any full node or via typical broadcast flow) and race a cheap TRX transfer ahead of it. No special privilege, staking, or witness/committee role is required — a single signed `TransferContract` from any funded account suffices.

### Recommendation
Align the top-level `VMActuator.create()` existence check with the internal `CREATE`/`CREATE2` handling in `Program.createContractImpl`: only reject deployment when a contract (code/`ContractCapsule`) already exists at the target address, not merely when any plain account happens to have been created there. If a plain account is found, upgrade it in place to a contract account (as already done for the internal CREATE path and for `HistoryBlockHashUtil.deploy`), preserving its balance rather than failing the whole transaction.

### Proof of Concept
1. Deployer builds and signs a `CreateSmartContract` transaction `T` for owner address `O`; compute `addr = sha3omit12(txRawDataHash(T) || O)` per `WalletUtil.generateContractAddress`.
2. Attacker observes `T` in the mempool (or otherwise learns `txRawDataHash(T)` and `O`), computes the same `addr`, and broadcasts a `TransferContract` sending 1 sun to `addr` with sufficient fee/priority to land in an earlier block than `T`.
3. When `T` is applied, `rootRepository.getAccount(contractAddress)` in `VMActuator.create()` is now non-null, so `ContractValidateException("Trying to create a contract with existing contract address: ...")` is thrown, and `T` fails every time it is resubmitted (its txID/derived `addr` never change since raw data is fixed after signing), forcing the deployer to construct an entirely new transaction to escape the griefed address.

### Citations

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
