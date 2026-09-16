### Title
Create-New-Account fee bypass via TVM value transfers to non-existent addresses - (File: actuator/src/main/java/org/tron/core/vm/program/Program.java)

### Summary
java-tron charges a governance-configurable `CREATE_NEW_ACCOUNT_FEE_IN_SYSTEM_CONTRACT` fee whenever a plain `TransferContract`/`TransferAssetContract` transaction implicitly creates a new account by sending TRX/TRC10 to a previously-unregistered address. The exact same account-creation state change can be produced through a smart contract executing `transfer`/`send`/`transferToken`/`call{value:}`/`suicide` to a non-existent address, but that path only debits a fixed, hardcoded energy surcharge instead of the configured TRX fee — the fee can be sidestepped entirely by routing the value transfer through a contract instead of a system `TransferContract`, exactly analogous to the reported "fee avoidable via an alternate function/entry point" bug class.

### Finding Description
When a `TransferContract` or `TransferAssetContract` creates a brand-new account for the `to_address`, it explicitly adds `dynamicStore.getCreateNewAccountFeeInSystemContract()` to the fee that is burned/sent to the blackhole: [1](#0-0) [2](#0-1) 

This value is a committee-adjustable chain parameter (`CREATE_NEW_ACCOUNT_FEE_IN_SYSTEM_CONTRACT`, range `[0, 100000000000]` sun) intended to prevent cheap/spam creation of new accounts through direct transfers: [3](#0-2) 

However, when a deployed smart contract performs the same state change — sending TRX or a TRC10 token to a non-existent address via `transfer`/`send`/`transferToken`/`call{value}`/`selfdestruct` — the TVM path creates the destination account via `Program.createAccountIfNotExist`, which unconditionally calls `deposit.createNormalAccount(contextAddress)` with **no reference at all** to `getCreateNewAccountFeeInSystemContract()`: [4](#0-3) 

The only cost imposed on this path is a fixed opcode-level energy surcharge (`EnergyCost.getNewAcctCall()`), confirmed by test assertions that the energy delta between calling an existing vs. non-existing address equals this constant: [5](#0-4) [6](#0-5) 

`EnergyCost.getNewAcctCall()` is a hardcoded constant unrelated to the governance-settable `CREATE_NEW_ACCOUNT_FEE_IN_SYSTEM_CONTRACT` value, so if the committee raises that fee to deter account-creation spam (up to 100,000 TRX per the proposal range), any user can simply deploy or reuse a contract that performs a `transfer`/`transferToken`/`call{value}`/`suicide` to the target unregistered address and create the account while paying only the fixed, much smaller energy price — completely bypassing the intended TRX-denominated fee, mirroring the reported "fee can be avoided via an alternate path" bug class (`lock` vs. `increaseLiquidity` in the report).

### Impact Explanation
This does not directly steal or freeze funds, but it defeats a protocol-level economic control (`CREATE_NEW_ACCOUNT_FEE_IN_SYSTEM_CONTRACT`) that the committee can tune specifically to prevent cheap/spam account creation (state-bloat / DoS mitigation). Since the fee can be trivially and completely bypassed by anyone with access to a deployed contract, the fee provides no real protection once its value diverges from the fixed TVM energy cost, undermining the committee's ability to control account-creation costs via the intended parameter. This is a medium-severity economic/anti-abuse-control-bypass issue rather than a fund-theft or crash issue.

### Likelihood Explanation
Trivially and reliably reachable by any unprivileged account: deploy (or reuse) a simple contract, fund it with a small TRX/TRC10 balance, and call a function that does `payable(addr).transfer(x)` or `IERC20/transferToken` to an unregistered address, or trigger `selfdestruct(addr)`. No special privileges, timing, or race conditions are required — this is a deterministic, repeatable bypass available to any transaction broadcaster.

### Recommendation
When `Program.createAccountIfNotExist` creates a new account as a side effect of a TVM value/token transfer, deduct `dynamicStore.getCreateNewAccountFeeInSystemContract()` (or an equivalent fee) from the calling contract's balance / the transaction's energy budget, consistent with the fee applied in `TransferActuator`/`TransferAssetActuator`, so that the account-creation fee cannot be bypassed simply by routing the transfer through contract code.

### Proof of Concept
1. Committee raises `CREATE_NEW_ACCOUNT_FEE_IN_SYSTEM_CONTRACT` to a non-trivial value via proposal.
2. Attacker deploys a simple contract with a payable function `send(address payable to) public payable { to.transfer(msg.value); }`.
3. Attacker calls `send(newUnregisteredAddress)` with a small TRX amount.
4. `Program.createAccountIfNotExist` (`actuator/src/main/java/org/tron/core/vm/program/Program.java:1893-1901`) creates the account, charging only the fixed `EnergyCost.getNewAcctCall()` energy surcharge — the `CREATE_NEW_ACCOUNT_FEE_IN_SYSTEM_CONTRACT` fee is never applied, unlike the equivalent `TransferContract` path in `TransferActuator.java:48-58`.

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

**File:** actuator/src/main/java/org/tron/core/actuator/TransferAssetActuator.java (L62-71)
```java
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

**File:** actuator/src/main/java/org/tron/core/utils/ProposalUtil.java (L956-958)
```java
    WITNESS_STANDBY_ALLOWANCE(6), // 115200 TRX, [0, 100000000000] TRX
    CREATE_NEW_ACCOUNT_FEE_IN_SYSTEM_CONTRACT(7), // 0 TRX, [0, 100000000000] TRX
    CREATE_NEW_ACCOUNT_BANDWIDTH_RATE(8), // 1 Bandwith/Byte, [0, 100000000000000000] Bandwith/Byte
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L1893-1901)
```java
  private void createAccountIfNotExist(Repository deposit, byte[] contextAddress) {
    if (VMConfig.allowTvmSolidity059()) {
      //after solidity059 proposal , allow contract transfer trc10 or TRX to non-exist address(would create one)
      AccountCapsule sender = deposit.getAccount(contextAddress);
      if (sender == null) {
        deposit.createNormalAccount(contextAddress);
      }
    }
  }
```

**File:** framework/src/test/java/org/tron/common/runtime/vm/TransferToAccountTest.java (L173-176)
```java
    long energyCostWhenNonExist = runtime.getResult().getEnergyUsed();
    //4.Test Energy
    Assert.assertEquals(energyCostWhenNonExist - energyCostWhenExist,
        EnergyCost.getNewAcctCall());
```

**File:** framework/src/test/java/org/tron/common/runtime/vm/TransferToAccountTest.java (L209-211)
```java
    //7.test energy
    Assert.assertEquals(energyCostWhenNonExist - energyCostWhenExist,
        EnergyCost.getNewAcctCall());
```
