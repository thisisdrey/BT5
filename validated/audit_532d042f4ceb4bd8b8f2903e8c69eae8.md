I found a strong analog: `freezeAction` and `unfreezeAction` in `OperationActions.java` guard their state-changing TVM opcodes with the `isStaticCall()` check **only conditionally**, gated behind `VMConfig.allowTvmVote()`, unlike every other resource/stake-modifying opcode (`freezeBalanceV2Action`, `unfreezeBalanceV2Action`, `withdrawExpireUnfreezeAction`, `cancelAllUnfreezeV2Action`, `delegateResourceAction`, `unDelegateResourceAction`, `voteWitnessAction`, `withdrawRewardAction`, `createAction`, `create2Action`) which check `program.isStaticCall()` unconditionally. [1](#0-0) 

### Title
`STATICCALL`-context state mutation via `FREEZE`/`UNFREEZE` TVM opcodes when `allowTvmVote` is disabled - (File: actuator/src/main/java/org/tron/core/vm/OperationActions.java)

### Summary
The FREEZE (`0xd5`) and UNFREEZE (`0xd6`) TVM opcodes in `OperationActions.freezeAction`/`OperationActions.unfreezeAction` only enforce the static-call (read-only) restriction when the `allowTvmVote` hard fork flag is enabled: `if (VMConfig.allowTvmVote() && program.isStaticCall())`. On any deployment/fork state where `allowTvmVote()` is `0`/`false`, a contract executed via `STATICCALL` can still invoke `FREEZE`/`UNFREEZE`, mutating account frozen-balance/resource state despite being inside a supposedly read-only call frame — the same "guard condition is incomplete/bypassable" root-cause pattern as the external report's regex blocklist that only covered some spellings of the dangerous call.

### Finding Description
Every other TVM state-mutating custom opcode added for staking/resource operations enforces `program.isStaticCall()` unconditionally before mutating state, e.g. `freezeBalanceV2Action`, `unfreezeBalanceV2Action`, `withdrawExpireUnfreezeAction`, `cancelAllUnfreezeV2Action`, `delegateResourceAction`, `unDelegateResourceAction`, `voteWitnessAction`, `withdrawRewardAction`: [2](#0-1) 

But the two legacy opcodes `freezeAction` and `unfreezeAction` wrap that same check in an extra condition that requires the `allowTvmVote` proposal to be active: [1](#0-0) 

`isStaticCall()` is what enforces the EVM/TVM invariant that code reached through a `STATICCALL` (opcode `0xfa`) must not modify state — this is the same invariant the `sStoreAction`, `logAction`, `createAction`, etc. enforce unconditionally elsewhere in the same file: [3](#0-2) 

If `allowTvmVote` is not yet activated on a given network/fork height (it is a `VMConfig` maintenance-committee-controlled hard-fork flag, same category as `allowTvmFreezeV2`, `allowTvmCompatibleEvm`, etc., toggled elsewhere in this file/class), a malicious contract can be called through `STATICCALL` from another contract or invoked as a "constant"/view call path, and still execute `FREEZE`/`UNFREEZE`, which internally calls `program.freeze(...)`/`program.unfreeze(...)` to move TRX into/out of frozen balance and grant/revoke bandwidth or energy — a state mutation that should be impossible inside a static context. This mirrors the reported bug class exactly: a safety check exists and is applied consistently almost everywhere, but one code path uses a narrower/gated version of the same check, silently permitting the prohibited operation when the extra condition isn't met — analogous to the blocklist regex only catching some spellings of `pg_read_file`.

### Impact Explanation
A successful bypass allows unauthorized mutation of on-chain account state (frozen balance / resource grants) from what should be a side-effect-free, non-committing execution context (`STATICCALL`/constant call). Depending on how call results from static contexts are committed by the surrounding `Program`/`Repository` machinery, this can lead to unauthorized account operations (freezing/unfreezing TRX, altering bandwidth/energy resource state) that violate the guarantees relied upon by other contracts and off-chain callers who use `STATICCALL`/`eth_call`-style constant calls expecting no state changes — a caller-side integrity violation with resource/fund-accounting impact when `allowTvmVote` is not (yet) enabled on the network.

### Likelihood Explanation
Reachable by any account that can deploy or call a smart contract (`TriggerSmartContract`) — no special privilege is required. The attacker only needs a contract that performs a `STATICCALL` to a contract using the `FREEZE`/`UNFREEZE` opcodes (directly emittable via inline assembly / raw bytecode, not gated by a Solidity-level guard) while the `allowTvmVote` proposal is disabled for the target chain. Because `allowTvmVote` is a globally-configured hard-fork switch (not caller-controlled), likelihood depends on the deployed network's activation state, which lowers exploitability on fully-upgraded mainnet but remains directly reachable on any chain/fork where that specific proposal has not been activated.

### Recommendation
Remove the `VMConfig.allowTvmVote() &&` gating in `freezeAction` and `unfreezeAction` so the static-call check `if (program.isStaticCall()) { throw new Program.StaticCallModificationException(); }` is applied unconditionally, consistent with every other state-mutating opcode in `OperationActions.java` (`sStoreAction`, `logAction`, `createAction`, `create2Action`, `freezeBalanceV2Action`, `unfreezeBalanceV2Action`, `voteWitnessAction`, etc.).

### Proof of Concept
1. Deploy Contract B containing raw bytecode that executes opcode `FREEZE` (`0xd5`) — e.g. push `resourceType`, `frozenBalance`, `receiverAddress` then emit byte `0xd5`.
2. Deploy Contract A that performs `STATICCALL` to Contract B (opcode `0xfa`), e.g. via inline assembly `staticcall(gas(), B, 0, 0, 0, 0)`.
3. On a network/test harness where `VMConfig.allowTvmVote()` returns `false` (default before that proposal is activated — see toggling pattern used in `OperationsTest.java`, e.g. `VMConfig.initAllowTvmVote(0)`), trigger Contract A.
4. Observe that Contract B's `FREEZE` executes successfully and mutates the target account's frozen balance/resource state, instead of throwing `Program.StaticCallModificationException()` as happens for the analogous `FREEZEBALANCEV2` opcode under the same static-call condition.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/OperationActions.java (L600-610)
```java
  public static void sStoreAction(Program program) {
    if (program.isStaticCall()) {
      throw new Program.StaticCallModificationException();
    }

    DataWord addr = program.stackPop();
    DataWord value = program.stackPop();

    program.storageSave(addr, value);
    program.step();
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/OperationActions.java (L791-822)
```java
  public static void freezeAction(Program program) {
    // after allow vote, check static
    if (VMConfig.allowTvmVote() && program.isStaticCall()) {
      throw new Program.StaticCallModificationException();
    }
    // 0 as bandwidth, 1 as energy
    DataWord resourceType = program.stackPop();
    DataWord frozenBalance = program.stackPop();
    DataWord receiverAddress = program.stackPop();

    if (VMConfig.allowTvmFreezeV2()) {
      // after v2 activated, we just push zero to stack and do nothing
      program.stackPush(DataWord.ZERO());
    } else {
      boolean result = program.freeze(receiverAddress, frozenBalance, resourceType );
      program.stackPush(result ? DataWord.ONE() : DataWord.ZERO());
    }
    program.step();
  }

  public static void unfreezeAction(Program program) {
    if (VMConfig.allowTvmVote() && program.isStaticCall()) {
      throw new Program.StaticCallModificationException();
    }

    DataWord resourceType = program.stackPop();
    DataWord receiverAddress = program.stackPop();

    boolean result = program.unfreeze(receiverAddress, resourceType);
    program.stackPush(result ? DataWord.ONE() : DataWord.ZERO());
    program.step();
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/OperationActions.java (L833-857)
```java
  public static void freezeBalanceV2Action(Program program) {
    // after allow vote, check static
    if (program.isStaticCall()) {
      throw new Program.StaticCallModificationException();
    }
    DataWord resourceType = program.stackPop();
    DataWord frozenBalance = program.stackPop();

    boolean result = program.freezeBalanceV2(frozenBalance, resourceType);
    program.stackPush(result ? DataWord.ONE() : DataWord.ZERO());
    program.step();
  }

  public static void unfreezeBalanceV2Action(Program program) {
    if (program.isStaticCall()) {
      throw new Program.StaticCallModificationException();
    }

    DataWord resourceType = program.stackPop();
    DataWord unfreezeBalance = program.stackPop();

    boolean result = program.unfreezeBalanceV2(unfreezeBalance, resourceType);
    program.stackPush(result ? DataWord.ONE() : DataWord.ZERO());
    program.step();
  }
```
