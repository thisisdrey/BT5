### Title
Stale account state (permissions, votes, frozen/delegated resources) survives contract self-destruct and is inherited by a CREATE2 redeploy at the same address - ([File: actuator/src/main/java/org/tron/core/vm/program/Program.java])

### Summary
When a TVM contract self-destructs via `SELFDESTRUCT` and the beneficiary is itself (`owner == obtainer`) on a contract that is *not* newly created in the current transaction, `Program.suicide2()` marks the address as self-destructed but skips vote-clearing, balance transfer, and frozen-resource cleanup entirely. If that same address is later reused as a CREATE2 target, `Program.createContractImpl()` only calls `existingAccount.clearDelegatedResource()` before reusing the pre-existing `AccountCapsule`, leaving votes, frozen-for-bandwidth/energy balances, and TRON power fields intact on the "new" contract.

### Finding Description
In `Program.suicide2()`: [1](#0-0) 
When `isNewContract` is false (i.e. the contract being destroyed was deployed in a prior transaction) and the beneficiary equals the contract itself, the method only calls `markSelfDestruct(owner)` and returns — it never calls `withdrawRewardAndCancelVote`, never clears frozen balances/delegated resources, and never calls `getResult().addDeleteAccount(...)`. Compare with the `suicide()` path used for brand-new contracts, which unconditionally clears votes via `withdrawRewardAndCancelVote` and registers the address for deletion: [2](#0-1) 

Later, when a contract is (re)created at that same address via CREATE2 (`createContractImpl`), the existing `AccountCapsule` is reused and only `clearDelegatedResource()` is invoked — which only zeroes the *acquired delegated* resource fields, not votes, not frozen-for-bandwidth/energy, not TRON power: [3](#0-2) [4](#0-3) 

This is structurally analogous to CVE-2021-32726: an entity is "destroyed" but residual credential/state (there: WebAuthn tokens tied to a username; here: votes, frozen resource, TRON power tied to an address) is not purged, and a subsequently (re)created entity at that same identifier inherits it.

### Impact Explanation
A contract redeployed via CREATE2 at a previously self-destructed address can inherit non-zero `votesList` and frozen-balance fields from the destroyed predecessor without those values being backed by any real, currently-frozen TRX. Because vote counting in `MaintenanceManager.countVote()`/`Wallet.countVote()` and `VoteWitnessProcessor`/`UnfreezeBalanceV2Processor` operate purely on the `AccountCapsule`'s stored vote and frozen-balance fields, stale votes can continue to influence SR vote totals (and reward distribution) for TRON power that no longer legitimately exists at that address — an unbacked-balance/vote condition. This can also interact with `UnfreezeBalanceV2Processor`/`UnDelegateResourceActuator`, which already contain defensive comments acknowledging "a TVM contract suicide, re-create will produce this situation" for delegated resources, confirming the authors were aware of resource-residue issues in this exact code path but did not extend the fix to votes/TRON-power/frozen fields.

### Likelihood Explanation
Reaching this requires only standard, permissionless TVM operations: deploying a factory contract, using CREATE2 to deploy a contract at a chosen salt, having that contract vote (`allowTvmVote`) or freeze for bandwidth/energy, then self-destructing to itself (`SELFDESTRUCT` with beneficiary == self) once it is no longer a "new" contract (i.e., in a transaction after the one that created it), and finally redeploying via CREATE2 with the same salt/factory/initcode to land on the identical address. All of these are standard, unprivileged TVM opcodes/precompiles reachable by any contract deployer — no special privileges are needed. The existing test suite (`FreezeTest.testCreate2SuicideToBlackHole`/`testCreate2SuicideToAccount`, `OperationsTest.testSuicideAction2`) demonstrates this exact create→freeze→suicide→recreate sequence is a recognized and exercised code path, increasing confidence it is reachable in practice, though I was not able to fully trace every intermediate commit/flush point (e.g., exact interaction with `RepositoryImpl` snapshot layering and `MaintenanceManager.countVote()` timing) to conclusively prove the end-to-end vote-inflation outcome within the available context.

### Recommendation
In `Program.suicide2()`, for the `owner == obtainer` branch on non-new contracts, clear votes (`withdrawRewardAndCancelVote`), frozen-for-bandwidth/energy balances, TRON power, and unfrozen-V2 lists before/at `markSelfDestruct`, mirroring what `suicide()` already does. Additionally, in `createContractImpl`, when reusing an `existingAccount` at a CREATE2 address, clear votes and all frozen/TRON-power fields (not just delegated resource fields) via `AccountCapsule.clearVotes()`, `clearOwnerFreeze`/`clearOwnerFreezeV2`-equivalent resets, before `updateAccountType(Contract)`.

### Proof of Concept
1. Deploy a `Factory` contract F.
2. From F, use CREATE2 with salt `S` and init-code `C` to deploy contract `A` at deterministic address `addr`.
3. Call into `A` to freeze balance for bandwidth/energy and to cast TVM votes (`allowTvmVote`), so `AccountCapsule(addr)` accumulates `votesList` and frozen balances.
4. In a *separate* transaction (so `isNewContract(addr)` is false), have `A` execute `SELFDESTRUCT(addr)` (beneficiary = itself). This hits the `owner == obtainer` branch in `suicide2()`, which only calls `markSelfDestruct` — votes/frozen fields remain in the stored `AccountCapsule`.
5. From F, redeploy via CREATE2 with the same salt `S`/init-code `C` to `addr` again. `createContractImpl` finds `existingAccount != null`, `!contractAlreadyExists`, and only calls `clearDelegatedResource()` — the reused capsule still carries the stale `votesList`/frozen balances.
6. Observe that the new contract's account participates in vote counting (`MaintenanceManager.countVote`) or resource accounting with values not backed by any newly frozen TRX at `addr`.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L451-518)
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
    if (VMConfig.allowTvmFreeze()) {
      byte[] blackHoleAddress = getContractState().getBlackHoleAddress();
      if (FastByteComparisons.isEqual(owner, obtainer)) {
        transferDelegatedResourceToInheritor(owner, blackHoleAddress, getContractState());
      } else {
        transferDelegatedResourceToInheritor(owner, obtainer, getContractState());
      }
    }
    if (VMConfig.allowTvmFreezeV2()) {
      byte[] Inheritor =
          FastByteComparisons.isEqual(owner, obtainer)
              ? getContractState().getBlackHoleAddress()
              : obtainer;
      long expireUnfrozenBalance = transferFrozenV2BalanceToInheritor(owner, Inheritor, getContractState());
      if (expireUnfrozenBalance > 0 && internalTx != null) {
        internalTx.setValue(internalTx.getValue() + expireUnfrozenBalance);
      }
    }

    getContractState().markSelfDestruct(owner);
    getResult().addDeleteAccount(this.getContractAddress());
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L527-551)
```java
    boolean isNewContract = getContractState().isNewContract(owner);
    if (isNewContract) {
      suicide(obtainerAddress);
      return;
    }

    byte[] obtainer = obtainerAddress.toTronAddress();

    long balance = getContractState().getBalance(owner);

    if (logger.isDebugEnabled()) {
      logger.debug("Transfer to: [{}] heritage: [{}]",
          Hex.toHexString(obtainer),
          balance);
    }

    increaseNonce();

    InternalTransaction internalTx = addInternalTx(null, owner, obtainer, balance, null,
        "suicide", nonce, getContractState().getAccount(owner).getAssetMapV2());

    if (FastByteComparisons.isEqual(owner, obtainer)) {
      getContractState().markSelfDestruct(owner);
      return;
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

**File:** chainbase/src/main/java/org/tron/core/capsule/AccountCapsule.java (L1326-1337)
```java
  // just for vm create2 instruction
  public void clearDelegatedResource() {
    Builder builder = account.toBuilder();
    AccountResource newAccountResource = getAccountResource().toBuilder()
        .setAcquiredDelegatedFrozenBalanceForEnergy(0L)
            .setAcquiredDelegatedFrozenV2BalanceForEnergy(0L)
            .build();
    builder.setAccountResource(newAccountResource);
    builder.setAcquiredDelegatedFrozenBalanceForBandwidth(0L)
            .setAcquiredDelegatedFrozenV2BalanceForBandwidth(0L);
    this.account = builder.build();
  }
```
