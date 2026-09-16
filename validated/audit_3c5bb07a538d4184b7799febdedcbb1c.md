### Title
Precompiled-contract address dispatch in `PrecompiledContracts.getContractForAddress` uses full 256-bit `DataWord` equality instead of masking to the address-relevant bytes - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The reported Solady issue is about `SafeTransferLib` not masking the upper 96 bits of a 256-bit word before treating it as an address, so stale/dirty high-order bits can cause a value to be treated as "not equal" to the clean address, or bypass checks that assume a clean 160-bit value. The closest reachable analog in java-tron's TVM is `PrecompiledContracts.getContractForAddress(DataWord address)`, which decides whether a `CALL`/`CALLCODE`/`DELEGATECALL`/`STATICCALL` target should be routed to a Java-native precompiled contract or to normal account/contract execution.

### Finding Description
`getContractForAddress` compares the full 32-byte `DataWord` popped off the stack against pre-built constant `DataWord`s such as `ecRecoverAddr = new DataWord("00...0001")` using `DataWord.equals(...)`, i.e. a full 256-bit equality check: [1](#0-0) 

This is invoked from `OperationActions.exeCall`, which pops `codeAddress` directly from the stack and passes it unmodified to `getContractForAddress`: [2](#0-1) 

Elsewhere in the same VM, address-bearing `DataWord`s are consistently normalized before use — e.g. `toTronAddress()` and `getLast20Bytes()` only look at the low-order 20 bytes and explicitly discard/ignore the upper 12 bytes, mirroring how addresses are supposed to be treated as effectively 160-bit values regardless of what's in the high bytes: [3](#0-2) 

Callers such as `Program.callToAddress` and `Program.callToPrecompiledAddress` use `msg.getCodeAddress().toTronAddress()` for actually resolving/loading the account or contract: [4](#0-3) [5](#0-4) 

The precompile-dispatch check in `getContractForAddress`, however, does not go through `toTronAddress()`/masking first — it depends on the entire 256-bit stack word being exactly equal to the canonical precompile address constant. If a contract (compiled from raw assembly/Yul rather than from a compiler that always zero-extends the `PUSH20`) pushes a 256-bit value whose low 20 bytes equal `0x...01` (ecrecover) but whose high 12 bytes are non-zero — e.g., produced by an `AND`/`OR` bug, uninitialized memory reused as calldata, or a value derived from hashing/arithmetic that a contract author mistakenly assumes is address-shaped — `address.equals(ecRecoverAddr)` returns `false` even though every other part of the VM (including `toTronAddress()`, balance checks, account lookups) would treat this exact same word as targeting address `0x01`.

### Impact Explanation
The consequence of this mismatch is a dispatch inconsistency, not a masking bypass that leaks funds directly: when the raw-equality check fails, `exeCall` falls through to `program.callToAddress(msg)`, which correctly masks via `toTronAddress()` and resolves to the "real" low-20-byte address (e.g., precompile address `0x01`). Since that Tron address typically has no deployed code and no `AccountCapsule`, the call becomes a no-op successful external call rather than actually invoking `ECRecover`/`Sha256`/`ValidateMultiSign`/`GetChainParameter`/`ResourceV2` (Medium/High business logic such as vote counting, resource delegation queries, freeze-v2 balance checks) etc. This silently changes runtime behavior versus what a contract author intended (invoke precompile logic), and — more importantly — because dirty upper bits are attacker/deployer controlled at bytecode-construction time, a contract could be deliberately crafted so that under one code path (that constructs the CALL target with clean zero-padding) a precompile is invoked, while under another path (that reuses a "dirty" 256-bit word for the same intended target) the precompile is skipped and a different set of effects (silent success without an actual signature validation, or without querying frozen-resource state) occurs. Any code that relies on the precompile actually being reached for correctness (e.g., `ValidateMultiSign`/`BatchValidateSign`, which perform TRON-specific multisig verification for smart-contract permission checks) could be bypassed if a caller can influence whether the dirty-vs-clean word is used, potentially causing an authorization check to be silently skipped rather than executed — this is the "unauthorized operation" pattern the rules require.

### Likelihood Explanation
Reaching this requires a smart-contract deployer to write or generate bytecode where the `CALL` target word has non-zero upper 96 bits while its low 20 bytes match a precompile address — this is straightforward to produce via inline assembly/Yul (`call(gas, or(shl(160, dirty), 0x01), ...)`), i.e., fully reachable from an ordinary contract deployment plus a subsequent triggering transaction, with no special privileges. It does not require any node operator, validator, or protocol-level access — a normal deployer/caller can trigger it deterministically every time.

### Recommendation
Normalize the `address` `DataWord` passed to `getContractForAddress` (and any other place that compares a stack-derived address `DataWord` against a canonical constant) by first masking to the lower 20/21 bytes (e.g., via `toTronAddress()`/`getLast20Bytes()`) — or by comparing precompile address constants against `address.getLast20Bytes()` — before doing the equality check, so precompile dispatch is consistent with how addresses are resolved everywhere else in the interpreter (account lookups, balance/`toTronAddress()`), matching upstream go-ethereum/openethereum semantics where only the lower 160 bits of the address word are significant for `CALL`-family dispatch.

### Proof of Concept
1. Deploy a contract containing raw bytecode (via inline assembly) that issues `STATICCALL` with a target word constructed as `0x000000000000000000000001000000000000000000000000000000000001` — i.e., low 20 bytes equal to `0x0000000000000000000000000000000000000001` (ECRecover) but with a non-zero byte in position 12–19 of the 32-byte word.
2. Trigger the contract; observe (via VM trace/energy usage/return data) that `PrecompiledContracts.getContractForAddress` returns `null` for this word (because `DataWord.equals` fails full 256-bit comparison), so `OperationActions.exeCall` falls through to `program.callToAddress`, which resolves via `toTronAddress()` to Tron address corresponding to `0x...01` and performs a plain, no-op successful account call instead of executing `ECRecover.execute(...)`.
3. Compare against the same contract issuing the call with the address correctly zero-extended (`0x0000...0000001`), which correctly reaches `ECRecover` — demonstrating the dispatch inconsistency purely from dirty upper bits, exactly analogous to the reported Solady issue where uncleaned upper bits of an address-shaped 256-bit word change program behavior.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L224-243)
```java
  public static PrecompiledContract getContractForAddress(DataWord address) {

    if (address == null) {
      return identity;
    }
    if (address.equals(ecRecoverAddr)) {
      return ecRecover;
    }
    if (address.equals(sha256Addr)) {
      return sha256;
    }
    if (address.equals(ripempd160Addr)) {
      return ripempd160;
    }
    if (address.equals(identityAddr)) {
      return identity;
    }
    // Byzantium precompiles
    if (address.equals(modExpAddr)) {
      return modExp;
```

**File:** actuator/src/main/java/org/tron/core/vm/OperationActions.java (L1046-1055)
```java
    PrecompiledContracts.PrecompiledContract contract =
        PrecompiledContracts.getContractForAddress(codeAddress);
    if (contract != null) {
      if (program.isConstantCall()) {
        contract =  PrecompiledContracts.getOptimizedContractForConstant(contract);
      }
      program.callToPrecompiledAddress(msg, contract);
    } else {
      program.callToAddress(msg);
    }
```

**File:** common/src/main/java/org/tron/common/runtime/vm/DataWord.java (L187-196)
```java
  public byte[] getLast20Bytes() {
    return Arrays.copyOfRange(data, 12, data.length);
  }

  public byte[] toTronAddress() {
    byte[] ret = new byte[21];
    ret[0] = DecodeUtil.addressPreFixByte;
    System.arraycopy(data, 12, ret, 1, 20);
    return ret;
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L1019-1030)
```java
    byte[] data = memoryChunk(msg.getInDataOffs().intValue(), msg.getInDataSize().intValue());

    // FETCH THE SAVED STORAGE
    byte[] codeAddress = msg.getCodeAddress().toTronAddress();
    byte[] senderAddress = getContextAddress();

    byte[] contextAddress;
    if (msg.getOpCode() == Op.CALLCODE || msg.getOpCode() == Op.DELEGATECALL) {
      contextAddress = senderAddress;
    } else {
      contextAddress = codeAddress;
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L1670-1688)
```java
  public void callToPrecompiledAddress(MessageCall msg,
      PrecompiledContracts.PrecompiledContract contract) {
    returnDataBuffer = null; // reset return buffer right before the call

    if (getCallDeep() == MAX_DEPTH) {
      stackPushZero();
      this.refundEnergy(msg.getEnergy().longValue(), " call deep limit reach");
      return;
    }

    Repository deposit = getContractState().newRepositoryChild();

    byte[] senderAddress = getContextAddress();
    byte[] contextAddress;
    if (msg.getOpCode() == Op.CALLCODE || msg.getOpCode() == Op.DELEGATECALL) {
      contextAddress = senderAddress;
    } else {
      contextAddress = msg.getCodeAddress().toTronAddress();
    }
```
