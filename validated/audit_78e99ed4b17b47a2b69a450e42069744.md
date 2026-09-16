## Title
STATICCALL invariant bypass in legacy FREEZE/UNFREEZE TVM opcodes — the state‑mutation guard is conditioned on an unrelated feature flag instead of being applied unconditionally - (File: `actuator/src/main/java/org/tron/core/vm/OperationActions.java`)

## Summary
The Apache Superset bug (CVE-2024-55633) is a case of a "read-only" classifier being applied inconsistently, letting a caller that is supposed to be restricted to non-mutating operations perform a write. The java-tron TVM has the same structural pattern in its `STATICCALL` enforcement: nearly every state-mutating opcode unconditionally checks `program.isStaticCall()` and throws `Program.StaticCallModificationException()` if true, but the legacy `freezeAction`/`unfreezeAction` opcodes gate that same check behind an unrelated proposal flag (`VMConfig.allowTvmVote()`), so the guard can be bypassed depending on network configuration.

## Finding Description
`Program.isStaticCall()` is the TVM's enforcement mechanism for the EVM/TVM `STATICCALL` invariant: code invoked via `STATICCALL` (nested call, set at `actuator/src/main/java/org/tron/core/vm/program/Program.java:1148`, `msg.getOpCode() == Op.STATICCALL || isStaticCall()`) must not be able to mutate any chain state. Every state-changing opcode in `OperationActions.java` enforces this uniformly and unconditionally, e.g.: [1](#0-0) [2](#0-1) [3](#0-2) 

However, the legacy freeze-resource opcodes only apply the check when a separate, semantically unrelated feature flag is active: [4](#0-3) [5](#0-4) 

`freezeAction`/`unfreezeAction` only throw `StaticCallModificationException` `if (VMConfig.allowTvmVote() && program.isStaticCall())`. If `allowTvmVote` (a committee-controlled proposal, off by default per `common/src/main/resources/reference.conf`) is not active on a given network while the legacy FREEZE/UNFREEZE opcodes are otherwise reachable (i.e., `allowTvmFreezeV2` not yet activated, since when it is active `freezeAction`/`unfreezeAction` become no-ops that just push zero, per lines 801-807), a contract invoked through `STATICCALL` from another contract can still execute `program.freeze(...)`/`program.unfreeze(...)`, mutating frozen-balance/resource state — exactly the class of bug the classifier in the Superset advisory exhibits: a mutation path that is supposed to be blocked under a "read-only" execution mode is only blocked conditionally, not structurally.

By contrast, the newer V2 equivalents (`freezeBalanceV2Action`, `unfreezeBalanceV2Action`, `withdrawExpireUnfreezeAction`, `cancelAllUnfreezeV2Action`) correctly enforce the check unconditionally: [6](#0-5) 

showing the intended, consistent pattern that the legacy V1 opcodes deviate from.

## Impact Explanation
If reachable (network with `allowTvmFreezeV2` not yet enabled and `allowTvmVote` not enabled, but freeze opcodes otherwise wired into the jump table), a maliciously crafted callee contract invoked via `STATICCALL` from a victim contract could freeze/unfreeze TRX balances and alter bandwidth/energy resource accounting despite the caller's explicit `staticcall` semantics guaranteeing no state change. This breaks a security invariant relied upon by dApps performing "safe" read-only external calls (e.g. price oracles, view-only integrations), and can result in unauthorized resource/stake state mutation from a call path the caller believed could not modify state.

## Likelihood Explanation
Exploitability is conditional on chain configuration: it requires `allowTvmVote` to be inactive while the FREEZE/UNFREEZE V1 opcodes are still live (i.e., `allowTvmFreezeV2` not yet activated). On current TRON mainnet, `allowTvmFreezeV2` has been enabled for some time, which makes `freezeAction`/`unfreezeAction` no-ops (they just push zero without calling `program.freeze`/`program.unfreeze`), reducing real-world impact on mainnet today. The vulnerability is real and reachable on any private/test chain, or any chain state, where these two flags are set independently in the vulnerable combination, and represents an inconsistent implementation of an otherwise universally-enforced invariant.

## Recommendation
Make the `isStaticCall()` check in `freezeAction` and `unfreezeAction` unconditional (drop the `VMConfig.allowTvmVote()` guard), consistent with every other state-mutating opcode and with the V2 equivalents, so the STATICCALL invariant cannot depend on an unrelated proposal flag.

## Proof of Concept
Not independently executed; the vulnerability path is derived directly from static code inspection of `actuator/src/main/java/org/tron/core/vm/OperationActions.java` lines 791-822 versus lines 600-610, 833-857, 869-967, showing the inconsistent conditional gating described above. Conceptually: deploy Contract A that performs `STATICCALL` to Contract B; Contract B's bytecode executes the `FREEZE`/`UNFREEZE` opcode; on a chain with `allowTvmVote=0` and `allowTvmFreezeV2=0`, the call proceeds without a `StaticCallModificationException`, and `program.freeze`/`program.unfreeze` executes, mutating account frozen-balance state despite the `STATICCALL` context.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/OperationActions.java (L600-603)
```java
  public static void sStoreAction(Program program) {
    if (program.isStaticCall()) {
      throw new Program.StaticCallModificationException();
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/OperationActions.java (L791-809)
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
```

**File:** actuator/src/main/java/org/tron/core/vm/OperationActions.java (L811-822)
```java
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

**File:** actuator/src/main/java/org/tron/core/vm/OperationActions.java (L833-837)
```java
  public static void freezeBalanceV2Action(Program program) {
    // after allow vote, check static
    if (program.isStaticCall()) {
      throw new Program.StaticCallModificationException();
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/OperationActions.java (L846-857)
```java
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

**File:** actuator/src/main/java/org/tron/core/vm/OperationActions.java (L931-934)
```java
  public static void createAction(Program program) {
    if (program.isStaticCall()) {
      throw new Program.StaticCallModificationException();
    }
```
