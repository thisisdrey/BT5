### Title
Null pointer dereference crash in TVM CALL/CALLCODE/DELEGATECALL/STATICCALL execution via `Program.callToAddress` when target account has code but no `ContractCapsule` - (File: actuator/src/main/java/org/tron/core/vm/program/Program.java)

### Summary
When `allowTvmCompatibleEvm` is enabled, `Program.callToAddress` unconditionally dereferences the result of `invoke.getDeposit().getContract(codeAddress)` to read its contract version, without a null check, analogous to the unchecked-lock-object dereference behind CVE-2026-29169's mod_dav_lock crash.

### Finding Description
In the internal-call path used for every `CALL`, `CALLCODE`, `DELEGATECALL`, and `STATICCALL` TVM opcode, once code is found for the target address, the code proceeds to construct a nested `Program` and, if `VMConfig.allowTvmCompatibleEvm()` is on, sets the contract version by chaining: [1](#0-0) 

```
Program program = new Program(programCode, codeAddress, programInvoke, internalTx);
program.setRootTransactionId(this.rootTransactionId);
if (VMConfig.allowTvmCompatibleEvm()) {
  program.setContractVersion(invoke.getDeposit()
      .getContract(codeAddress).getContractVersion());
}
```

The presence check that gates this whole call branch only verifies that an `AccountCapsule` exists and that `getCode(codeAddress)` returns non-empty bytes: [2](#0-1) 

```
// FETCH THE CODE
AccountCapsule accountCapsule = getContractState().getAccount(codeAddress);

byte[] programCode =
    accountCapsule != null ? getContractState().getCode(codeAddress) : EMPTY_BYTE_ARRAY;
```

Code presence (`CodeStore`/account code) and `ContractCapsule` presence (`ContractStore`) are backed by separate stores. The gating logic never confirms that `getContract(codeAddress)` returns a non-null `ContractCapsule` before calling `.getContractVersion()` on it. If an address has non-empty code but no corresponding `ContractCapsule` entry — which can occur for accounts whose code was written through a path that does not also populate the contract store, or where the two stores can otherwise diverge (e.g., partial state inconsistency, contract records removed/pruned while code remains, or a codeAddress that intentionally is not a standard `CreateSmartContract`-deployed contract) — `getContract(codeAddress)` returns `null`, and `.getContractVersion()` throws a `NullPointerException`.

This exactly mirrors the mod_dav_lock bug class: a code path assumes an internal object (the lock / the `ContractCapsule`) is always present whenever a superficially related precondition holds (a lock header / non-empty code), but that assumption is not actually enforced, so a request that reaches the mismatched state triggers an unguarded NPE.

### Impact Explanation
An uncaught `NullPointerException` thrown from deep within TVM opcode execution (`VM.play` -> `Program.callToAddress`) is not one of the caught, expected VM exceptions (`ContractValidateException`, `ContractExeException`, etc.) that the actuator/executor framework gracefully converts to a failed receipt. If it propagates uncaught out of transaction processing in `Manager` during block application or during `TriggerConstantContract`/`triggerContract` handling, it can crash the node process or leave the node in a broken state, denying service. Because block validators must all execute the same transaction, a reproducible NPE trigger embedded in a smart-contract-invoking transaction could be broadcast to the whole network, causing simultaneous crashes across nodes — a network-wide denial-of-service condition reachable purely via a signed transaction from any unprivileged account, matching the "node crash" acceptance criterion.

### Likelihood Explanation
Exploitability depends on being able to reach a target `codeAddress` that has non-empty code in the code/account store but no corresponding entry in the contract store, while `allowTvmCompatibleEvm` is enabled (an activated TVM feature on mainnet/most networks). This is a state-dependent precondition rather than a directly attacker-controlled data value, so likelihood is Medium: it requires either a specific account/contract inconsistency to already exist in chain state, or another path (e.g., interaction with precompiles, selfdestruct/CREATE2 edge cases, or genesis-provisioned code) that can produce code without a matching `ContractCapsule`. I was not able to fully verify from the available code excerpts whether such a divergent state can be produced today by an ordinary user transaction — this requires deeper review of `RepositoryImpl.getContract`/`getCode`, `ContractStore`, and `CodeStore` population logic, and of any code path that writes to one store without the other, to construct a concrete transaction sequence.

### Recommendation
Add a null check on the result of `invoke.getDeposit().getContract(codeAddress)` before calling `.getContractVersion()` in `Program.callToAddress` (and audit any other unguarded `getContract(...).getXxx()` chains in `Program.java`/`VMActuator.java`, per the `setContractVersion` call sites found in those files). If the `ContractCapsule` is null, default to a safe legacy contract version or skip the version assignment, rather than dereferencing a potentially-null object.

### Proof of Concept
Not fully constructible from the indexed code alone: reproducing the crash requires demonstrating a concrete on-chain state where `codeAddress` has non-empty code (so `isNotEmpty(programCode)` is true) but `getContract(codeAddress)` returns `null`, while `VMConfig.allowTvmCompatibleEvm()` is enabled. This would need direct inspection of `RepositoryImpl`/`ContractStore`/`CodeStore` write paths (not fully covered by the retrieved snippets) to identify a concrete transaction or contract-deployment sequence producing that divergence. I recommend a Devin session with full repository access to trace `RepositoryImpl.getContract`, `RepositoryImpl.getCode`, and all `ContractStore`/`CodeStore` `put`/`delete` call sites to confirm whether such a divergent state is reachable from an unprivileged transaction, and if so, build the exact PoC transaction sequence.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L1078-1082)
```java
    // FETCH THE CODE
    AccountCapsule accountCapsule = getContractState().getAccount(codeAddress);

    byte[] programCode =
        accountCapsule != null ? getContractState().getCode(codeAddress) : EMPTY_BYTE_ARRAY;
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L1153-1158)
```java
      Program program = new Program(programCode, codeAddress, programInvoke, internalTx);
      program.setRootTransactionId(this.rootTransactionId);
      if (VMConfig.allowTvmCompatibleEvm()) {
        program.setContractVersion(invoke.getDeposit()
            .getContract(codeAddress).getContractVersion());
      }
```
