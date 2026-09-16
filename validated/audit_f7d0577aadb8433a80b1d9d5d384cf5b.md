## Title
Forced TRX/TRC10 balance injection into arbitrary contracts via `SELFDESTRUCT` bypasses the `ForbidTransferToContract` protection - (File: `actuator/src/main/java/org/tron/core/vm/program/Program.java`, `actuator/src/main/java/org/tron/core/vm/utils/MUtil.java`)

### Summary
The Lido report describes a class of bug where an unrelated privileged component can force-alter the balance/shares of another contract without going through that contract's own accounting/validation path, breaking an internal invariant and permanently blocking a legitimate user-facing function (`unwrap`). In java-tron, the analogous, unprivileged-reachable path is the `SELFDESTRUCT`/`SUICIDE` opcode handling in the TVM: it force-transfers the destructing contract's TRX balance and *all* of its TRC10 token balances to an arbitrary "obtainer" address, using `MUtil.transfer`/`MUtil.transferAllToken`, which never consult the `ForbidTransferToContract` dynamic property that every other user-facing asset/TRX transfer path enforces.

### Finding Description
`TransferAssetActuator.validate()` explicitly forbids sending TRC10 assets to a smart-contract address once the `ForbidTransferToContract` proposal is active: [1](#0-0) 

However, `Program.suicide()` / `Program.suicide2()` — the implementation of the `SUICIDE`/`SELFDESTRUCT` opcode, reachable by any contract call from an unprivileged transaction — forcibly moves the caller-contract's entire TRX balance and its whole TRC10 asset map to any address supplied on the stack, via `MUtil.transfer(...)` and `MUtil.transferAllToken(...)`: [2](#0-1) [3](#0-2) 

`MUtil.transfer` only calls `VMUtils.validateForSmartContract` (which checks balances/overflow, not the `ForbidTransferToContract` flag), and `MUtil.transferAllToken` performs no validation at all — it just merges every TRC10 balance from the destructing account into the target: [4](#0-3) 

Because these routines bypass the check present in `TransferAssetActuator`/`VMUtils.validateForSmartContract` for `getForbidTransferToContract()`, any deployed contract can be made to `SELFDESTRUCT` (destination address fully attacker-controlled) toward any other deployed smart contract, injecting arbitrary TRX and TRC10 token balances into it without executing the target's code/fallback and without the target contract's knowledge or consent.

### Impact Explanation
This is the same "forced balance mutation underneath a contract's own accounting" bug class as the Lido report: any TVM contract that maintains internal invariants tied to `address(this).balance` or its own TRC10 token holdings (e.g., a wrapping/vault-style contract, an escrow, an AMM pool, or any contract that asserts `balance == internal accounting` before executing a withdrawal/unwrap-style function) can have its balance forcibly and permanently inflated by an attacker via `SELFDESTRUCT` from a throwaway contract, without the victim contract ever being invoked. This can permanently break invariant checks and freeze legitimate user-facing functions in such contracts — a permanent freezing-of-funds condition for any downstream contract relying on exact balance matching.

### Likelihood Explanation
Deploying a small helper contract that calls `selfdestruct(target)` (or `SELFDESTRUCT` opcode directly) is trivially reachable by any unprivileged contract deployer/transaction broadcaster; no special permission, SR/witness/committee status, or privileged account is required. The bypass is deterministic — `MUtil.transfer`/`MUtil.transferAllToken` in the suicide path never call the `ForbidTransferToContract` check that `TransferAssetActuator` enforces for the same effective operation (crediting TRC10/TRX to a contract).

### Recommendation
Add the same `ForbidTransferToContract`-style validation (or an explicit design decision that self-destruct beneficiaries being contracts is acceptable) inside `Program.suicide`/`suicide2` before invoking `MUtil.transfer`/`MUtil.transferAllToken`, or otherwise ensure downstream vault/wrapper-style contracts are documented as needing to defend against forced balance injection (i.e., not assert strict equality between tracked and actual on-chain balance).

### Proof of Concept
1. Deploy `ContractA` with a fallback/function that calls `selfdestruct(ContractB)` where `ContractB` is a victim contract that tracks its own TRX/TRC10 balances internally (e.g., asserts `address(this).balance == internalAccounting` before allowing a withdraw-like function).
2. Fund `ContractA` with TRX and/or TRC10 tokens.
3. Trigger `ContractA`'s self-destruct function pointing at `ContractB`. `Program.suicide2()` executes `MUtil.transfer` and `MUtil.transferAllToken`, crediting `ContractB` with `ContractA`'s full TRX balance and all TRC10 token balances — bypassing any `ForbidTransferToContract` restriction and without calling any of `ContractB`'s code.
4. Observe that `ContractB`'s on-chain balance no longer matches its internal accounting, causing any invariant check in `ContractB` (analogous to Lido's `unwrap`) to permanently revert/fail.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/TransferAssetActuator.java (L169-175)
```java
    AccountCapsule toAccount = accountStore.get(toAddress);
    if (toAccount != null) {
      //after ForbidTransferToContract proposal, send trx to smartContract by actuator is not allowed.
      if (dynamicStore.getForbidTransferToContract() == 1
          && toAccount.getType() == AccountType.Contract) {
        throw new ContractValidateException("Cannot transfer asset to smartContract.");
      }
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L451-496)
```java
  public void suicide(DataWord obtainerAddress) {

    byte[] owner = getContextAddress();
    byte[] obtainer = obtainerAddress.toTronAddress();

    if (VMConfig.allowTvmVote()) {
      withdrawRewardAndCancelVote(owner, getContractState());
    }

    long balance = getContractState().getBalance(owner);

    if (logger.isDebugEnabled()) {
      logger.debug("Transfer to: [{}] heritage: [{}]",
          Hex.toHexString(obtainer),
          balance);
    }

    increaseNonce();

    InternalTransaction internalTx = addInternalTx(null, owner, obtainer, balance, null,
        "suicide", nonce, getContractState().getAccount(owner).getAssetMapV2());

    int ADDRESS_SIZE = VMUtils.getAddressSize();
    if (FastByteComparisons.compareTo(owner, 0, ADDRESS_SIZE, obtainer, 0, ADDRESS_SIZE) == 0) {
      // if owner == obtainer just zeroing account according to Yellow Paper
      getContractState().addBalance(owner, -balance);
      byte[] blackHoleAddress = getContractState().getBlackHoleAddress();
      if (VMConfig.allowTvmTransferTrc10()) {
        getContractState().addBalance(blackHoleAddress, balance);
        MUtil.transferAllToken(getContractState(), owner, blackHoleAddress);
      }
    } else {
      createAccountIfNotExist(getContractState(), obtainer);
      try {
        MUtil.transfer(getContractState(), owner, obtainer, balance);
        if (VMConfig.allowTvmTransferTrc10()) {
          MUtil.transferAllToken(getContractState(), owner, obtainer);
        }
      } catch (ContractValidateException e) {
        if (VMConfig.allowTvmConstantinople()) {
          throw new TransferException(
              "transfer all token or transfer all trx failed in suicide: %s", e.getMessage());
        }
        throw new BytecodeExecutionException("transfer failure");
      }
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L561-574)
```java
    // transfer balance and trc10
    createAccountIfNotExist(getContractState(), obtainer);
    try {
      MUtil.transfer(getContractState(), owner, obtainer, balance);
      if (VMConfig.allowTvmTransferTrc10()) {
        MUtil.transferAllToken(getContractState(), owner, obtainer);
      }
    } catch (ContractValidateException e) {
      if (VMConfig.allowTvmConstantinople()) {
        throw new TransferException(
            "transfer all token or transfer all trx failed in suicide: %s", e.getMessage());
      }
      throw new BytecodeExecutionException("transfer failure");
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/utils/MUtil.java (L18-41)
```java
  public static void transfer(Repository deposit, byte[] fromAddress, byte[] toAddress, long amount)
      throws ContractValidateException {
    if (0 == amount) {
      return;
    }
    VMUtils.validateForSmartContract(deposit, fromAddress, toAddress, amount);
    deposit.addBalance(toAddress, amount);
    deposit.addBalance(fromAddress, -amount);
  }

  public static void transferAllToken(Repository deposit, byte[] fromAddress, byte[] toAddress) {
    AccountCapsule fromAccountCap = deposit.getAccount(fromAddress);
    Protocol.Account.Builder fromBuilder = fromAccountCap.getInstance().toBuilder();
    AccountCapsule toAccountCap = deposit.getAccount(toAddress);
    toAccountCap.importAllAsset();
    Protocol.Account.Builder toBuilder = toAccountCap.getInstance().toBuilder();
    fromAccountCap.getAssetMapV2().forEach((tokenId, amount) -> {
      toBuilder.putAssetV2(tokenId, toBuilder.getAssetV2Map().getOrDefault(tokenId, 0L) + amount);
      fromBuilder.putAssetV2(tokenId, 0L);
    });

    deposit.putAccountValue(fromAddress, new AccountCapsule(fromBuilder.build()));
    deposit.putAccountValue(toAddress, new AccountCapsule(toBuilder.build()));
  }
```
