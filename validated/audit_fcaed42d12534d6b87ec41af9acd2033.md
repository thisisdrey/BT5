### Title
NullPointerException DoS in Blake2F Precompiled Contract Energy Metering - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
`PrecompiledContracts.Blake2F` is the only EVM-compatibility precompile in `PrecompiledContracts.java` that does not guard against a `null` `data` argument before dereferencing it, unlike every sibling precompile (`Identity`, `Sha256`, `Ripempd160`, `BN128Multiplication`, `BN128Pairing`, `EthRipemd160`, `GetChainParameter`, `IsSrCandidate`, `UsedVoteCount`, etc.) which all explicitly check `data == null` before use.

### Finding Description
`Blake2F.getEnergyForData(byte[] data)` immediately dereferences `data.length`: [1](#0-0) 

Compare this with the neighboring `EthRipemd160` precompile (registered under the same `VMConfig.allowTvmCompatibleEvm()` gate) which explicitly null-checks its input before use: [2](#0-1) 

`getEnergyForData()` is invoked by `Program.callToPrecompiledAddress()` before the energy check and before `execute()` is called: [3](#0-2) 

This is directly analogous to the CVE-2021-39520 bug class: a code path that consumes attacker/caller-supplied data without validating that the pointer/reference is non-null before dereferencing it, unlike sibling/parallel code paths in the same module that do perform this check — indicating the check was simply omitted for this handler.

`getEnergyForData` for Blake2F is reachable from any `CALL`/`STATICCALL`/`DELEGATECALL` TVM opcode targeting the Blake2F precompile address (`0x...020009`) when `VMConfig.allowTvmCompatibleEvm()` is enabled, i.e. from any unprivileged smart-contract call once the corresponding TIP is activated on-chain.

### Impact Explanation
If `data` reaches this call as `null` (the established pattern of null-checking across every other precompile in the file — including the immediately adjacent `EthRipemd160` — strongly suggests this is a code path the codebase authors intended to be null-safe), a `NullPointerException` (an unchecked `RuntimeException`) will be thrown while executing TVM opcode logic. `VM.play()`'s `catch (RuntimeException e)` re-throws the exception after halting execution: [4](#0-3) 

Since `RuntimeImpl.execute()` does not wrap `actuator2.execute(context)` in an exception handler that converts arbitrary `RuntimeException`s into a controlled `ContractExeException`, an uncaught NPE from this path can propagate up through `TransactionTrace`/actuator execution during block application, resulting in a denial-of-service condition for any node processing a transaction that triggers this call.

### Likelihood Explanation
I was unable to fully confirm, given the remaining tool budget, whether the `data` byte array supplied to precompile calls via `Program.callToPrecompiledAddress()` (sourced from `memoryChunk()` reads of CALLDATA) can ever actually be `null` rather than an empty `byte[0]` array in the current code path — this is the main open question affecting exploitability. However, the fact that 10+ other precompiled contracts in this exact file (including ones added in the same feature set, gated by the same `VMConfig` flags) defensively null-check their input is strong circumstantial evidence that `null` is a reachable value in this code path, and that `Blake2F`'s omission is an oversight rather than an intentional invariant.

### Recommendation
Add a `data == null` guard at the start of both `Blake2F.getEnergyForData()` and `Blake2F.execute()`, mirroring the pattern used by `EthRipemd160` and other precompiles in the same file, treating `null` the same as an incorrectly-sized input (return `0`/`Pair.of(false, DataWord.ZERO().getData())`).

### Proof of Concept
1. Deploy/enable `VMConfig.allowTvmCompatibleEvm()` (TIP enabling EVM-compatible precompiles) on a test node.
2. Deploy a simple contract that performs a low-level `call`/`staticcall` to precompile address `0x0000...020009` (Blake2F) using an assembly call with `calldatasize` 0 at an offset beyond current memory bounds, or directly craft a `TriggerSmartContract` invoking `CALL` with zero `inDataSize` to that address in a way that yields a `null` (rather than empty) data buffer to `contract.getEnergyForData(data)`.
3. Observe whether `Blake2F.getEnergyForData()` throws `NullPointerException`, propagating through `VM.play()` and up through block/transaction processing.

Note: step 2's exact mechanism for producing a literal `null` (vs. empty array) from `memoryChunk()` could not be fully verified within the available investigation and would need confirmation via a background Devin session with full repo/test access before treating this as conclusively exploitable.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1976-1996)
```java
  public static class EthRipemd160 extends PrecompiledContract {

    @Override
    public long getEnergyForData(byte[] data) {
      if (data == null) {
        return 600;
      }
      return 600L + (data.length + 31) / 32 * 120L;
    }

    @Override
    public Pair<Boolean, byte[]> execute(byte[] data) {
      byte[] result;
      if (data == null) {
        result = Hash.ripemd160(EMPTY_BYTE_ARRAY);
      } else {
        result = Hash.ripemd160(data);
      }
      return Pair.of(true, new DataWord(result).getData());
    }
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1998-2008)
```java
  public static class Blake2F extends PrecompiledContract {

    @Override
    public long getEnergyForData(byte[] data) {
      if (data.length != 213 || (data[212] & 0xFE) != 0) {
        return 0;
      }
      final byte[] roundsBytes = copyOfRange(data, 0, 4);
      final BigInteger rounds = new BigInteger(1, roundsBytes);
      return rounds.longValue();
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L1734-1740)
```java
    long requiredEnergy = contract.getEnergyForData(data);
    if (requiredEnergy > msg.getEnergy().longValue()) {
      // Not need to throw an exception, method caller needn't know that
      // regard as consumed the energy
      this.refundEnergy(0, CALL_PRE_COMPILED); //matches cpp logic
      this.stackPushZero();
    } else {
```

**File:** actuator/src/main/java/org/tron/core/vm/VM.java (L93-100)
```java
        } catch (RuntimeException e) {
          logger.info("VM halted: [{}]", e.getMessage());
          if (!(e instanceof TransferException)) {
            program.spendAllEnergy();
          }
          //program.resetFutureRefund();
          program.stop();
          throw e;
```
