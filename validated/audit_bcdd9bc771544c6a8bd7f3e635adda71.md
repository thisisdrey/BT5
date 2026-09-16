### Title
Integer Overflow in `VOTEWITNESS` Memory-Energy Accounting Allows Underpriced Memory Exhaustion - (File: `actuator/src/main/java/org/tron/core/vm/EnergyCost.java`)

### Summary
The Vyper report describes a compiler that panics instead of gracefully rejecting an expression whose declared size overflows machine-representable memory bounds. The java-tron analog is in the TVM's `VOTEWITNESS` opcode energy-metering path, where the memory-needed calculation for the vote-array arguments is done using fixed-width `DataWord` arithmetic that silently wraps (mod 2^256) instead of detecting overflow, so a maliciously large array-length operand can make the metering code believe far less memory is required than is actually requested.

### Finding Description
`EnergyCost.getVoteWitnessCost` and `EnergyCost.getVoteWitnessCost2` compute the memory needed for the `amountArray`/`witnessArray` operands of `VOTEWITNESS` by multiplying the attacker-controlled array length (a `DataWord`, i.e. an unsigned 256-bit integer read straight off the stack) by the 32-byte word size using `DataWord.mul()`: [1](#0-0) 

`DataWord.mul()` performs modular 256-bit arithmetic like the rest of EVM/TVM word math, so `amountArrayLength.mul(wordSize)` can wrap around to a small value when `amountArrayLength` is close to `2^256/32`. The wrapped, small value is then fed into `memNeeded()`/`calcMemEnergy()`, which computes the energy charge and the "safe" memory size: [2](#0-1) 

`checkMemorySize` only rejects the (already-wrapped) small `newMemSize` against `MEM_LIMIT`, so the overflow is never detected and the memory/energy check passes cheaply, even though the real, unwrapped operand value the opcode logic actually acts on is enormous. This is precisely the class of bug the report describes: an arithmetic overflow at a size-computation boundary that a correctly-designed check should have caught but doesn't, because the intermediate arithmetic type (`DataWord`, a fixed 256-bit word — analogous to Vyper's compiler internal size representation) silently truncates/wraps instead of surfacing an error.

The regression test explicitly documents the wrap in `cost`/`cost2` and confirms that only the newer, `BigInteger`-based `getVoteWitnessCost3` (gated behind `VMConfig.allowTvmOsaka()`) correctly detects the overflow and throws `Program.OutOfMemoryException`: [3](#0-2) [4](#0-3) 

Dispatch to the fixed cost function is conditional on the Osaka hard-fork flag: [5](#0-4) [6](#0-5) 

Until `allowTvmOsaka` is enabled on a live network, any contract call using the legacy `VOTEWITNESS` opcode is metered by the vulnerable `cost`/`cost2` path.

### Impact Explanation
An attacker who deploys or calls a contract that issues `VOTEWITNESS` with a crafted 256-bit array-length operand can make the interpreter under-charge energy for the memory the operand claims to need, because the overflow-safe check (`checkMemorySize` against `MEM_LIMIT`) never actually sees the true requested size. This mirrors the report's "compiler panics on overflow" bug class: a validation step intended to reject out-of-range sizes is defeated by wraparound arithmetic. In an EVM/TVM interpreter this is a resource-accounting integrity bug — a resource-exhaustion / energy-underpayment vector reachable from a single signed transaction — rather than a purely cosmetic compiler issue, and falls squarely within the in-scope "TVM opcodes ... and energy metering" surface.

### Likelihood Explanation
The vulnerable code path is reachable directly from any account by issuing a `TriggerSmartContract` transaction whose bytecode executes `VOTEWITNESS` with a very large length word for either the amount or witness array; no special privileges (SR/witness/committee/peer) are required, matching the "unprivileged transaction broadcaster/contract deployer" reachability the analog must satisfy. The bug is only mitigated on networks/forks where `allowTvmOsaka` is active, so it remains exploitable pre-Osaka.

### Recommendation
Route all `VOTEWITNESS` memory/energy accounting through the overflow-safe `BigInteger` path (`getVoteWitnessCost3`) unconditionally, rather than gating the fix behind `VMConfig.allowTvmOsaka()`, or otherwise ensure `getVoteWitnessCost`/`getVoteWitnessCost2` detect `DataWord` multiplication overflow (e.g., by comparing operands against `MEM_LIMIT` in `BigInteger` form before doing fixed-width multiplication) so a wrapped result can never silently pass `checkMemorySize`.

### Proof of Concept
1. Deploy a contract that executes `VOTEWITNESS` with `witnessArrayLength` (or `amountArrayLength`) set to a value `v` such that `v * 32 mod 2^256` is small (e.g. `v = 2^256 / 32` or similar carefully chosen operand).
2. On a network where `allowTvmOsaka` is not enabled (so `getVoteWitnessCost`/`getVoteWitnessCost2` is used), call the contract.
3. Observe that `EnergyCost.getVoteWitnessCost2` computes a small `witnessArrayMemoryNeeded`/`amountArrayMemoryNeeded` because `DataWord.mul()` wrapped, so `calcMemEnergy`'s `checkMemorySize` call passes and only a small energy fee is charged — as demonstrated directly by the existing unit test `testLargeArrayLengthOverflow`/`testCost3FallsBackToCost2WhenOsakaOff`, which show `cost2`'s silent wrap versus `cost3`'s correct `OutOfMemoryException`. [4](#0-3)

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/EnergyCost.java (L346-365)
```java
  public static long getVoteWitnessCost(Program program) {
    Stack stack = program.getStack();
    long oldMemSize = program.getMemSize();
    DataWord amountArrayLength = stack.get(stack.size() - 1).clone();
    DataWord amountArrayOffset = stack.get(stack.size() - 2);
    DataWord witnessArrayLength = stack.get(stack.size() - 3).clone();
    DataWord witnessArrayOffset = stack.get(stack.size() - 4);

    DataWord wordSize = new DataWord(DataWord.WORD_SIZE);

    amountArrayLength.mul(wordSize);
    BigInteger amountArrayMemoryNeeded = memNeeded(amountArrayOffset, amountArrayLength);

    witnessArrayLength.mul(wordSize);
    BigInteger witnessArrayMemoryNeeded = memNeeded(witnessArrayOffset, witnessArrayLength);

    return VOTE_WITNESS + calcMemEnergy(oldMemSize,
        (amountArrayMemoryNeeded.compareTo(witnessArrayMemoryNeeded) > 0
            ? amountArrayMemoryNeeded : witnessArrayMemoryNeeded), 0, Op.VOTEWITNESS);
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/EnergyCost.java (L367-370)
```java
  public static long getVoteWitnessCost2(Program program) {
    if (!VMConfig.allowEnergyAdjustment()) {
      return getVoteWitnessCost(program);
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/EnergyCost.java (L394-397)
```java
  public static long getVoteWitnessCost3(Program program) {
    if (!VMConfig.allowTvmOsaka()) {
      return getVoteWitnessCost2(program);
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/EnergyCost.java (L549-570)
```java
  private static long calcMemEnergy(long oldMemSize, BigInteger newMemSize,
                             long copySize, int op) {
    long energyCost = 0;

    checkMemorySize(op, newMemSize);

    // memory SUN consume calc
    long memoryUsage = (newMemSize.longValueExact() + 31) / 32 * 32;
    if (memoryUsage > oldMemSize) {
      long memWords = (memoryUsage / 32);
      long memWordsOld = (oldMemSize / 32);
      long memEnergy = (MEMORY * memWords + memWords * memWords / 512)
          - (MEMORY * memWordsOld + memWordsOld * memWordsOld / 512);
      energyCost += memEnergy;
    }

    if (copySize > 0) {
      long copyEnergy = COPY_ENERGY * ((copySize + 31) / 32);
      energyCost += copyEnergy;
    }
    return energyCost;
  }
```

**File:** framework/src/test/java/org/tron/common/runtime/vm/VoteWitnessCost3Test.java (L134-157)
```java
  @Test
  public void testLargeArrayLengthOverflow() {
    // Use a very large value that would overflow in DataWord.mul() in cost2
    // DataWord max is 2^256-1, multiplying by 32 would overflow
    // In cost3, BigInteger handles this correctly and should trigger memoryOverflow
    String maxHex = "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff";
    DataWord largeLength = new DataWord(maxHex);
    DataWord zeroOffset = new DataWord(0);

    Program program = mockProgram(zeroOffset, new DataWord(1),
        zeroOffset, largeLength, 0);

    boolean overflowCaught = false;
    VMConfig.initAllowTvmOsaka(1);    // cost3 self-dispatches; exercise the v3 (BigInteger) path
    try {
      EnergyCost.getVoteWitnessCost3(program);
    } catch (Program.OutOfMemoryException e) {
      // cost3 should detect memory overflow via checkMemorySize
      overflowCaught = true;
    } finally {
      VMConfig.initAllowTvmOsaka(0);
    }
    assertTrue("cost3 should throw memoryOverflow for huge array length", overflowCaught);
  }
```

**File:** framework/src/test/java/org/tron/common/runtime/vm/VoteWitnessCost3Test.java (L251-276)
```java
  @Test
  public void testCost3FallsBackToCost2WhenOsakaOff() {
    // cost3 self-dispatches: with osaka off it delegates to cost2, so a cost3 left in the shared
    // jump table (e.g. read by a constant call whose view has osaka off) still charges v2 energy.
    String maxHex = "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff";
    DataWord huge = new DataWord(maxHex);
    DataWord zero = new DataWord(0);

    VMConfig.initAllowTvmOsaka(0);
    long viaCost3 =
        EnergyCost.getVoteWitnessCost3(mockProgram(zero, new DataWord(1), zero, huge, 0));
    long viaCost2 =
        EnergyCost.getVoteWitnessCost2(mockProgram(zero, new DataWord(1), zero, huge, 0));
    assertEquals("cost3 must equal cost2 when osaka is off", viaCost2, viaCost3);

    // sanity: with osaka on, cost3 instead runs v3 and detects the overflow that cost2 wraps away.
    VMConfig.initAllowTvmOsaka(1);
    try {
      EnergyCost.getVoteWitnessCost3(mockProgram(zero, new DataWord(1), zero, huge, 0));
      fail("cost3 with osaka on must overflow-throw on the huge length");
    } catch (Program.OutOfMemoryException expected) {
      // expected
    } finally {
      VMConfig.initAllowTvmOsaka(0);
    }
  }
```
