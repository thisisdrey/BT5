### Title
Incomplete state sanitization on CREATE2/CREATE account-collision reuse allows a contract deployer to smuggle pre-existing votes, TRC10 balances, self-frozen resources and multisig permissions into a freshly "created" contract account - (File: actuator/src/main/java/org/tron/core/vm/program/Program.java)

### Summary
The reported Nextcloud bug is a class of "copy/absorb an existing object without checking/cleaning its dangerous pre-existing content." The closest reachable analog in java-tron is the CREATE/CREATE2 contract-creation path in `Program.createContractImpl`, which can *reuse* a pre-existing, attacker-controlled account at the deterministically-computable target address, but only strips two of its many mutable fields before treating it as a brand-new contract account.

### Finding Description
When a contract executes `CREATE`/`CREATE2`, `Program.createContractImpl` computes the target address and checks whether an account already exists there: [1](#0-0) 

If `existingAccount != null` but it is not itself an already-deployed contract (`isContractExist` returns false, i.e. it's a plain EOA that received TRX/assets/votes/permissions beforehand), the code only calls `existingAccount.updateAccountType(AccountType.Contract)` and `existingAccount.clearDelegatedResource()` before reusing that same `AccountCapsule` as the new contract's account: [2](#0-1) 

Because `CREATE2` addresses (and, with a brute-forced nonce, even `CREATE` addresses) are fully deterministic from `sender/salt/initcode`, any contract deployer can predict the address of a not-yet-deployed contract and, prior to deployment, send a transaction that:
- funds the address (self-freezes TRX for bandwidth/energy there),
- votes for SRs from that address,
- transfers TRC10 asset balances to it,
- sets a custom `owner_permission`/multisig configuration on it (`AccountPermissionUpdateContract`).

Only `clearDelegatedResource()` (which — based on its name and the codebase's own documentation comment in `HistoryBlockHashUtil.java` describing this exact code path — clears *delegated* resource fields) is invoked; nothing in the reused branch resets votes, TRC10 asset balances, self-frozen-for-self resource entries, or the account's permission structure. This mirrors the root cause of the Nextcloud report: an object (here, an `AccountCapsule`) is carried over into a new, supposedly-clean context (a freshly created smart-contract account) without validating/clearing state that is unsafe to carry over.

I was not able to fully verify, within the available tool budget, the exact downstream fields `clearDelegatedResource()` does and does not touch (specifically whether votes, self-frozen balances, and permission keys survive), nor whether any actuator subsequently blocks operations (e.g., `AccountPermissionUpdateContract`, vote/unfreeze actuators) once `AccountType` flips to `Contract`. This should be confirmed by inspecting `AccountCapsule.clearDelegatedResource()`/`updateAccountType()` in `chainbase/src/main/java/org/tron/core/capsule/AccountCapsule.java` and the `AccountType.Contract` guards in `VoteWitnessActuator`, `UnfreezeBalanceActuator`, and `AccountPermissionUpdateActuator`.

### Impact Explanation
If votes, self-frozen bandwidth/energy, TRC10 balances, or (most severely) multisig permission keys are not cleared, a deployer could:
- Make a newly-created contract "inherit" voting power or frozen resources it never legitimately staked, corrupting stake/vote accounting (an in-scope class: stake/delegation/reward math).
- Leave a stale `owner_permission`/multisig configuration attached to a contract-type address; if any actuator's signature-verification path does not additionally gate on `AccountType == Contract`, the original key holder could still authorize `TransferContract`/`UnfreezeBalanceContract` operations from what is nominally a contract address, causing unauthorized account operations or fund extraction.

### Likelihood Explanation
Likelihood is uncertain to Medium: triggering the collision itself is trivial and fully attacker-controlled (any contract deployer can pre-compute a CREATE2 address and fund/configure it before deployment — this requires no privileged role). What is unverified is whether any of the surviving fields (votes, permissions, frozen-for-self, TRC10 balances) are actually exploitable post-collision, since I could not confirm the exact scope of `clearDelegatedResource()` or the account-type guards in the relevant actuators within this session.

### Recommendation
When absorbing a pre-existing EOA into a new contract account in `Program.createContractImpl`, fully sanitize the reused `AccountCapsule`: reset votes, TRC10 asset balances, self-frozen (v1 and v2) resource entries, and permission (`owner_permission`, `witness_permission`, `active_permission`) fields — not just delegated-resource fields — before marking it `AccountType.Contract`. Alternatively, reject the CREATE/CREATE2 outright (push zero / revert) whenever the target address holds any non-default account state beyond balance, rather than silently reusing it.

### Proof of Concept
Conceptual PoC (needs confirmation against `AccountCapsule` internals to be conclusive):
1. Deploy a factory contract with a `deploy(bytes code, uint256 salt)` using `CREATE2`, and off-chain compute the resulting address `addr` via `keccak256(0x41 || factory || salt || keccak256(initcode))` per `WalletUtil.generateContractAddress2`.
2. Before calling `deploy`, from an attacker-controlled key, send transactions to `addr`: `TransferContract` (fund it), `VoteWitnessContract` (vote), `AccountPermissionUpdateContract` (set custom multisig), `FreezeBalanceV2Contract` (self-freeze for bandwidth/energy).
3. Call `factory.deploy(initcode, salt)`; observe via `Program.createContractImpl` that `existingAccount != null` and `contractAlreadyExists == false`, taking the reuse branch that calls only `updateAccountType`/`clearDelegatedResource`.
4. Query the resulting contract account's votes, TRC10 balances, frozen-for-self entries, and permission fields via `Wallet.getAccount` and confirm which of them (if any) survived the conversion, and whether any actuator still accepts transactions signed under the stale permission on `addr`.

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
