## Analysis

The external report's bug class is: **a one-time "create" operation checks only for non-existence of a resource, and that resource's existence can be trivially established beforehand by an unprivileged third party via a completely separate, permissionless mechanism** — allowing griefing of the legitimate creation transaction.

The closest reachable analog in java-tron is `CreateAccountActuator.validate()`, which gates account creation purely on `accountStore.has(accountAddress)`, while any unrelated, unprivileged transaction (a plain TRX transfer) can implicitly create that same account entry beforehand. [1](#0-0) 

### Title
Griefing `AccountCreateContract` by pre-creating the target account address via a plain TRX transfer - (File: actuator/src/main/java/org/tron/core/actuator/CreateAccountActuator.java)

### Summary
`CreateAccountActuator.validate()` rejects account creation solely because `accountStore.has(accountAddress)` returns `true`, with the error `"Account has existed"`. Since java-tron auto-creates an `AccountCapsule` entry for any address the first time it receives a TRX transfer (standard `TransferActuator` behavior of implicitly creating accounts on first receipt), any unprivileged party who observes or predicts the intended `accountAddress` of a pending `AccountCreateContract` transaction can front-run it with a trivial-value TRX transfer to that address, permanently causing the legitimate `AccountCreateContract` transaction to fail with `ContractValidateException("Account has existed")`.

### Finding Description
`CreateAccountActuator` is invoked via the broadcastable `AccountCreateContract` transaction type, callable by anyone holding TRX for the fee. Its validation logic is: [1](#0-0) 

The check does not distinguish between an account that was created via an explicit `AccountCreateContract` (which may set a specific `AccountType`/name/default multisign permission via `execute()`) and an account entry that already exists merely because it was the recipient of a TRX transfer (an implicit `Normal` account created as a side effect elsewhere in the codebase). Because account existence in the `AccountStore` can be produced by a completely unrelated, permissionless action (sending any amount of TRX to that address), a malicious actor who can predict or observe the target `accountAddress` field of a not-yet-mined `AccountCreateContract` transaction (e.g., from the mempool, or because the address is deterministically derived off-chain, similar to how the UniswapV3 pool address is deterministic and reachable via `createPool` before `launchGroupCoinToUniswapV3`) can pre-empt it with a minimal-cost transfer, making the legitimate creation permanently fail.

This mirrors the report's root cause exactly: a "create" call is guarded only by an existence check on a resource key that a griefer can populate through an independent, permissionless entry point before the intended caller's transaction lands.

### Impact Explanation
The griefed party's `AccountCreateContract` transaction permanently reverts with `ContractValidateException`, and any account setup logic in `CreateAccountActuator.execute()` — including the explicit `AccountType`, account name, and default multisign permission assignment gated by `dynamicStore.getAllowMultiSign()` — never executes for that address: [2](#0-1) 

Any downstream service or protocol relying on this actuator to pre-provision accounts with specific attributes (e.g., custodial deposit-address provisioning, or applications requiring default multisign permissions to be set at creation time) can be denied that setup deterministically and irrecoverably for the targeted address, since the address will forever satisfy `accountStore.has(accountAddress)` once griefed.

### Likelihood Explanation
Likelihood is Medium: the attacker needs only (a) minimal TRX to send a transfer, and (b) knowledge of the target `accountAddress` before the `AccountCreateContract` transaction is confirmed — obtainable via mempool observation or if the address is derived by an off-chain, predictable scheme (as in the original report's deterministic pool address).

### Recommendation
Distinguish between an account that already underwent explicit `AccountCreateContract` processing versus one that exists only as an implicit balance-holder. For example, only reject if the existing `AccountCapsule` already has an assigned `AccountType` other than the default/implicit state, or track an explicit "created" flag separately from mere `AccountStore` presence, and allow `CreateAccountActuator.execute()` to apply the intended type/name/permission to a pre-existing implicit account rather than unconditionally failing — analogous to the report's suggested "initialize if not already initialized" pattern.

### Proof of Concept
1. Attacker observes a pending `AccountCreateContract` transaction in the mempool (or predicts the target address `A` via an off-chain deterministic scheme).
2. Attacker immediately broadcasts a `TransferContract` sending 1 sun to address `A`.
3. This transfer is processed first (or in the same block with higher priority), causing the `AccountStore` to contain an entry for `A` as a `Normal` account.
4. The original `AccountCreateContract` transaction targeting `A` is then validated: `accountStore.has(accountAddress)` at [3](#0-2)  returns `true`, and the transaction fails permanently with `"Account has existed"`, denying the intended account provisioning for `A`.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/CreateAccountActuator.java (L40-49)
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
