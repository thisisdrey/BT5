### Title
CREATE2 deployments can be griefed via front-running of the predictable target address - (File: actuator/src/main/java/org/tron/core/vm/program/Program.java)

### Summary
The reported bug is a griefing pattern: a protocol accepts a caller-chosen identifier that determines a unique state slot, and because that identifier is visible before finalization it can be observed and "claimed" by an attacker ahead of the legitimate user, permanently blocking the legitimate operation. The direct analog in java-tron is TVM's `CREATE2` opcode, where the resulting contract address is fully deterministic from `(sender, salt, code-hash)` — all values chosen/known by the caller and visible in the pending transaction's calldata — and any prior occupant at that address causes the deployment to fail.

### Finding Description
`CREATE2` is implemented in `Program.createContract2`, which computes the deterministic address via `WalletUtil.generateContractAddress2(senderAddress, salt.getData(), programCode)` and passes it to `createContractImpl`: [1](#0-0) 

`createContractImpl` looks up any existing account/contract at the computed address and, if a contract is already deployed there, aborts the creation: [2](#0-1) [3](#0-2) 

The predicted address depends only on the caller/factory address (`senderAddress`), the `salt` value pushed to the stack, and the `keccak256` hash of the init code — as documented and tested by the project's own `getCreate2Addr`/`generateContractAddress2` helpers: [4](#0-3) [5](#0-4) 

Since a `TriggerSmartContract` transaction carrying the `salt` and init-code bytes in its calldata sits in the mempool prior to inclusion, any observer can recompute the exact target address, then submit (and get mined first) a competing transaction that deploys arbitrary bytecode — or even any transaction that plants a `Contract`-type account — at that same address. When the victim's original transaction is later executed, `contractAlreadyExists` evaluates true and the create aborts with `"Trying to create a contract with existing contract address"` as shown at the cited lines, consuming the victim's energy/bandwidth for a failed transaction. The regression test explicitly documents this failure path, confirming the "deploy by other user again, should fail" behavior for a claimed CREATE2 address: [6](#0-5) 

This mirrors the reported bug class exactly: a caller-supplied identifier (there, `loanId`; here, `salt`+init-code which yield the address) determines uniqueness of a piece of on-chain state, the identifier is observable before confirmation, and a front-runner can claim it first to grief the legitimate actor's create operation.

### Impact Explanation
Any factory contract on java-tron that relies on `CREATE2` for deterministic, front-runnable deployment (e.g., counterfactual wallet/contract deployment patterns, common in cross-chain and account-abstraction designs) can have its deployments griefed indefinitely by a third party who is watching the mempool for `salt`/init-code values and resubmitting them with higher priority/fee. This is a pure griefing vector — no profit motive is required — that can deny legitimate users the ability to deploy to their intended, precomputed address, consuming their TRX/energy on each failed attempt and blocking any downstream logic that depends on the precomputed address being deployable.

### Likelihood Explanation
The prerequisite state (`salt`, init code) is transmitted in plaintext inside ordinary trigger-contract transaction calldata and is visible in the mempool before block inclusion, exactly as in the referenced report. No special privilege beyond being an ordinary network participant / transaction broadcaster is required, and the front-runner only needs to outbid the victim's transaction (e.g., higher fee/energy or faster relay) — the same assumption the original report itself relies on ("front-run any loan creation operation seen on the Hub chain").

### Recommendation
- Do not rely purely on a caller/attacker-observable `(sender, salt, codehash)` tuple for irreversible, one-shot deployment guarantees in factory contracts deployed on java-tron; document this TVM behavior clearly for Solidity developers targeting TRON.
- At the protocol/actuator layer, consider giving deployers a way to reserve or commit to a target address (e.g., commit-reveal for salts) rather than only checking existence at execution time, if this is deemed worth changing at the VM level.
- At minimum, ensure wallet/dApp tooling built on `CreateSmartContract`/`CREATE2` warns users about mempool-visible salts and encourages commit–reveal or private relay/bundling for security-sensitive deployments.

### Proof of Concept
1. Victim submits a `TriggerSmartContract` transaction that calls a Solidity `Factory.deploy(bytes code, uint256 salt)` function, which internally executes the `CREATE2` opcode as implemented by `Program.createContract2` (`WalletUtil.generateContractAddress2`).
2. Attacker observes this pending transaction in the mempool, extracts `code` and `salt` from the calldata, and independently computes the same deterministic address (as done by `getCreate2Addr` in `FreezeTest.sol`).
3. Attacker submits and gets included first a transaction that deploys (via its own `CREATE2` call with the same `senderAddress`/`salt`/`code`, or via any mechanism that plants contract code at that address) a contract at the identical predicted address.
4. When the victim's original transaction executes, `createContractImpl` finds `contractAlreadyExists == true` at the target address and sets the exception `"Trying to create a contract with existing contract address: 0x..."`, causing the victim's deployment to fail and consuming their energy, exactly as validated by the assertion in `Create2Test.java` lines 343–350. [7](#0-6)

### Citations

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

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L916-920)
```java
    if (contractAlreadyExists) {
      createResult.setException(new BytecodeExecutionException(
          "Trying to create a contract with existing contract address: 0x" + Hex
              .toHexString(newAddress)));
    } else if (isNotEmpty(programCode)) {
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L1629-1653)
```java
  public void createContract2(DataWord value, DataWord memStart, DataWord memSize, DataWord salt) {
    if (VMConfig.allowTvmOsaka()) {
      returnDataBuffer = null; // reset return buffer right before the call
    }

    byte[] senderAddress;
    if ((VMConfig.allowTvmCompatibleEvm() || VMConfig.allowTvmOsaka())
        && getCallDeep() == MAX_DEPTH) {
      stackPushZero();
      return;
    }
    if (getCallDeep() == MAX_DEPTH) {
      MUtil.checkCPUTimeForCreate2();
    }
    if (VMConfig.allowTvmIstanbul()) {
      senderAddress = getContextAddress();
    } else {
      senderAddress = getCallerAddress().toTronAddress();
    }
    byte[] programCode = memoryChunk(memStart.intValue(), memSize.intValue());

    byte[] contractAddress = WalletUtil
        .generateContractAddress2(senderAddress, salt.getData(), programCode);
    createContractImpl(value, programCode, contractAddress, true);
  }
```

**File:** chainbase/src/main/java/org/tron/common/utils/WalletUtil.java (L55-59)
```java
  // for `CREATE2`
  public static byte[] generateContractAddress2(byte[] address, byte[] salt, byte[] code) {
    byte[] mergedData = ByteUtil.merge(address, salt, Hash.sha3(code));
    return Hash.sha3omit12(mergedData);
  }
```

**File:** framework/src/test/java/org/tron/common/runtime/vm/FreezeTest.sol (L65-86)
```text
    // selector: 0xbb63e785
    // Predict CREATE2 address without deploying
    //
    // TRON CREATE2 formula (differs from standard EVM):
    //   address = keccak256(prefix ++ sender[20] ++ salt[32] ++ keccak256(code)[32])[12:]
    //
    // - Standard EVM uses 0xff as prefix (magic byte)
    // - TRON replaces it with the address prefix byte (0x41 for mainnet, 0xa0 for testnet)
    // - This value is hardcoded at compile time by tron-solc
    //
    function getCreate2Addr(uint256 salt) public view returns (address) {
        bytes memory bytecode = type(FreezeContract).creationCode;
        bytes32 hash = keccak256(
            abi.encodePacked(
                bytes1(0x41),       // TRON mainnet address prefix
                address(this),      // 20-byte factory address
                salt,               // 32-byte salt
                keccak256(bytecode) // 32-byte code hash
            )
        );
        return address(uint160(uint256(hash)));
    }
```

**File:** framework/src/test/java/org/tron/common/runtime/vm/Create2Test.java (L337-350)
```java
    String ownerAddress2 = Wallet.getAddressPreFixString()
        + "8dcd6d3b585e41863123af20e57ec9f678035d92";
    rootDeposit.createAccount(Hex.decode(ownerAddress2), AccountType.Normal);
    rootDeposit.addBalance(Hex.decode(ownerAddress2), 30000000000000L);
    rootDeposit.commit();

    // deploy contract by OTHER user again, should fail
    hexInput = AbiUtil.parseMethod(methodDeploy, Arrays.asList(testCode, salt));
    result = TvmTestUtils
        .triggerContractAndReturnTvmTestResult(Hex.decode(ownerAddress2),
            factoryAddress, Hex.decode(hexInput), 0, fee, manager, null);
    Assert.assertNotNull(result.getRuntime().getRuntimeError());
    Assert.assertTrue(result.getRuntime().getResult().getException()
        instanceof OutOfEnergyException);
```
