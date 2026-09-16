### Title
`AccountType.Contract` check in resource-delegation validation can be bypassed by delegating to a not-yet-deployed CREATE2 address - ([File: actuator/src/main/java/org/tron/core/vm/nativecontract/DelegateResourceProcessor.java])

### Summary
`DelegateResourceProcessor.validate()` (and the legacy `DelegateResourceActuator`/`FreezeBalanceActuator`) reject resource delegation to a receiver whose stored `AccountType` is `Contract`, intending to prevent bandwidth/energy from being delegated to smart-contract accounts. This is analogous to the reported `onlyEOAEx()` bug: the check relies on a point-in-time account-type flag rather than verifying that the target can never become a contract, so it can be trivially bypassed by targeting an address that is not yet marked `Contract` but is destined to become one.

### Finding Description
The relevant check: [1](#0-0) 

and the equivalent legacy path: [2](#0-1) 

Both only reject the receiver when `receiverCapsule.getType() == AccountType.Contract`. A TRON address can be pre-funded (e.g. via a plain TRX transfer) before the contract that will eventually occupy that address is deployed via `CREATE`/`CREATE2`; while pre-funded and un-deployed, `AccountCapsule#getType()` is `Normal`, not `Contract`. An attacker can therefore:
1. Determine the deterministic contract address in advance (`CREATE2`, or `CREATE` with known nonce).
2. Send a small TRX transfer to that address so an `AccountCapsule` with `AccountType.Normal` exists (satisfying the "receiver must exist" precondition).
3. Delegate bandwidth/energy resources to that address — the `AccountType.Contract` check passes because the account is still `Normal`.
4. Deploy the smart contract at that precomputed address.

The end state is a contract address holding delegated resources, which the check was explicitly designed to prevent (`"Do not allow delegate resources to contract addresses"`), just as the OpenZeppelin `isContract()`-based `onlyEOAEx()` modifier can be defeated by targeting an address before contract code is deployed there.

### Impact Explanation
This bypasses an explicit protocol invariant ("do not allow delegate resources to contract addresses") without any privileged access — any unprivileged user issuing ordinary `DelegateResourceContract`/`FreezeBalanceContract` transactions plus a normal deployment transaction can achieve it. This is a Medium-severity logic/invariant bypass rather than a fund-theft or node-crash bug: it does not corrupt balances, but it defeats a deliberate contract-address restriction that downstream logic (undelegate flows, resource accounting for delegated bandwidth/energy on contract accounts) assumes cannot happen.

### Likelihood Explanation
High reachability: every step (funding an address, delegating resources, deploying a contract to a precomputed address) is a normal, permissionless transaction type reachable by any external account; no special privileges, timing races, or validator cooperation are required. `CREATE2` counterfactual addressing is a standard, well-documented technique, making exploitation straightforward.

### Recommendation
Do not gate delegation solely on the current `AccountType`. Either:
- Re-validate (and revert/undo) delegation at contract-deployment time if the receiver already has delegated-in resources, or
- Track and check whether the destination address is a reserved/precomputed contract address before allowing delegation, or
- Reconsider whether restricting delegation to contract addresses is necessary at all, since (as with the original `onlyEOAEx` finding) "is this an EOA" checks based on a mutable, timing-dependent property cannot be made robust; consider removing the restriction and instead handling contract-owned delegated resources safely wherever they are consumed (undelegate, resource accounting).

### Proof of Concept
1. Precompute a `CREATE2` contract address `C` for a factory contract owned by attacker EOA `A` (standard `keccak256(0xff ++ deployer ++ salt ++ initCodeHash)` derivation, verifiable against TRON's `CREATE2` address algorithm as used in `WalletUtil.generateContractAddress`).
2. From `A`, submit a `TransferContract` sending 1 SUN to `C`. This creates an `AccountCapsule` at `C` with `AccountType.Normal`. [3](#0-2) 
3. From `A` (or any account with frozen v2 balance), submit a `DelegateResourceContract` with `receiverAddress = C`. `DelegateResourceProcessor.validate()` passes because `receiverCapsule.getType() != AccountType.Contract`. [1](#0-0) 
4. Deploy the contract at `C` via `CREATE2` from the factory, which upgrades the account's type to `Contract` in place (analogous to the pattern shown in `HistoryBlockHashUtil.deploy`, which upgrades a pre-existing `Normal` account to `Contract` while preserving balance). [4](#0-3) 
5. `C` is now a deployed contract holding delegated bandwidth/energy that the validate-time check was supposed to forbid.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/DelegateResourceProcessor.java (L104-114)
```java
    AccountCapsule receiverCapsule = repo.getAccount(receiverAddress);
    if (receiverCapsule == null) {
      String readableOwnerAddress = StringUtil.createReadableString(receiverAddress);
      throw new ContractValidateException(
          ActuatorConstant.ACCOUNT_EXCEPTION_STR
              + readableOwnerAddress + NOT_EXIST_STR);
    }
    if (receiverCapsule.getType() == Protocol.AccountType.Contract) {
      throw new ContractValidateException(
          "Do not allow delegate resources to contract addresses");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/FreezeBalanceActuator.java (L254-267)
```java
      AccountCapsule receiverCapsule = accountStore.get(receiverAddress);
      if (receiverCapsule == null) {
        String readableOwnerAddress = StringUtil.createReadableString(receiverAddress);
        throw new ContractValidateException(
            ActuatorConstant.ACCOUNT_EXCEPTION_STR
                + readableOwnerAddress + NOT_EXIST_STR);
      }

      if (dynamicStore.getAllowTvmConstantinople() == 1
          && receiverCapsule.getType() == AccountType.Contract) {
        throw new ContractValidateException(
            "Do not allow delegate resources to contract addresses");

      }
```

**File:** actuator/src/main/java/org/tron/core/actuator/TransferActuator.java (L1-2)
```java
package org.tron.core.actuator;

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
