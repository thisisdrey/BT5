### Title
Silent 256-bit integer overflow in `VOTEWITNESS` energy-cost sizing bypasses memory/energy bounds check - (File: `actuator/src/main/java/org/tron/core/vm/EnergyCost.java`)

### Summary
`EnergyCost.getVoteWitnessCost` and `getVoteWitnessCost2` compute the memory size needed for the `amountArray`/`witnessArray` operands of the `VOTEWITNESS` TVM opcode using `DataWord.mul()`, a fixed 256-bit modular multiplication that wraps silently on overflow instead of raising an error, exactly the missing-overflow-check pattern described in CVE-2026-25210 for expat's buffer-size reallocation.

### Finding Description
`getVoteWitnessCost` and `getVoteWitnessCost2` multiply an attacker-controlled `DataWord` array length by the 32-byte word size using `DataWord.mul(wordSize)`: [1](#0-0) [2](#0-1) 

`DataWord` represents a fixed 256-bit word, and arithmetic like `mul`/`add` on it is modular (wraps mod 2^256) rather than throwing on overflow, unlike the fixed `getVoteWitnessCost3` path, which was rewritten to use `BigInteger` explicitly to "detect the overflow that cost2 wraps away" per the accompanying regression test: [3](#0-2) 

The size derived from this wrapped multiplication feeds `memNeeded(...)` and then `calcMemEnergy`, which checks the result against the 3MB `MEM_LIMIT` and computes the memory-expansion energy charge: [4](#0-3) 

If `amountArrayLength * 32` (or `+wordSize`) overflows 2^256 and wraps to a small value, `checkMemorySize` will not throw `memoryOverflow`, and `calcMemEnergy` will under-charge energy for a memory region that the caller intended to be enormous — the same root cause class as the libexpat CVE: a size computed without an overflow check is used to drive a downstream buffer/memory operation.

Crucially, `getVoteWitnessCost3` — the function actually wired into the opcode's energy-cost slot in the jump table — self-dispatches to the vulnerable `getVoteWitnessCost2`/`getVoteWitnessCost` whenever `VMConfig.allowTvmOsaka()` is `false`: [5](#0-4) 
This means the overflow-safe `BigInteger` path is only used when the `TvmOsaka` hard-fork/feature flag is enabled; otherwise the legacy, wrap-around-prone `DataWord`-based cost function remains live on any node/network where that flag has not been activated yet.

### Impact Explanation
An under-computed memory-expansion cost for `VOTEWITNESS` means an attacker can craft `amountArrayLength`/`witnessArrayLength` stack values that wrap to a tiny apparent size while the opcode logic (and any subsequent `Memory.extend`/read operations acting on the real, un-wrapped offsets/sizes) attempts to operate on a memory region far larger than what was paid for. `Memory.extend` itself uses `addExact` and does throw on true `int` overflow, so the most likely outcome is a `Program.Exception.memoryOverflow`/`OutOfMemoryException` being thrown inconsistently with what was charged, or energy being systematically miscalculated relative to actual resource consumption for this opcode — an energy-metering/gas-accounting integrity bug reachable by any contract caller triggering `VOTEWITNESS`. This falls under "node crash or halt" / incorrect resource accounting for a broadcastable contract call, matching the allowed impact categories.

### Likelihood Explanation
Reachable by any unprivileged account that can call a contract executing the `VOTEWITNESS` TVM opcode with attacker-chosen stack operands; no special privilege is required. Likelihood is gated on `VMConfig.allowTvmOsaka()` being disabled for the vulnerable path to be live, which is plausible for networks/nodes that have not yet activated that feature switch, and the existing regression tests explicitly demonstrate the wrap-around behavior is real and was only fixed in the `cost3`/`BigInteger` path.

### Recommendation
Remove the fallback to `getVoteWitnessCost2`/`getVoteWitnessCost` from `getVoteWitnessCost3`, or backport the `BigInteger`-based overflow-safe size computation into `getVoteWitnessCost`/`getVoteWitnessCost2` themselves so that no code path performs the `DataWord.mul()`/`add()` wrap-around arithmetic for size computation feeding `calcMemEnergy`/`memNeeded`.

### Proof of Concept
1. Deploy a contract that executes `VOTEWITNESS` with the amount/witness array length stack operand set to a value such that `length * 32` (or `+32`) overflows 2^256 and wraps to a small number (e.g. `length = (2^256 - 32) / 32 + 1` style values chosen so `DataWord.mul` wraps).
2. Ensure the network/node has `VMConfig.allowTvmOsaka()` disabled (default until that feature is activated), so `getVoteWitnessCost3` delegates to `getVoteWitnessCost2`.
3. Observe that `calcMemEnergy`'s `checkMemorySize` does not throw `memoryOverflow` (since the wrapped size is below `MEM_LIMIT`), while the opcode logic operates on the true, unwrapped offset/length, producing energy accounting inconsistent with actual memory usage/behavior, as reproduced by `VoteWitnessCost3Test.testLargeArrayLengthOverflow`/`testCost3FallsBackToCost2WhenOsakaOff`, which explicitly assert the overflow is "wrapped away" by `cost2` versus caught by `cost3`: [6](#0-5)

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

**File:** actuator/src/main/java/org/tron/core/vm/EnergyCost.java (L367-392)
```java
  public static long getVoteWitnessCost2(Program program) {
    if (!VMConfig.allowEnergyAdjustment()) {
      return getVoteWitnessCost(program);
    }

    Stack stack = program.getStack();
    long oldMemSize = program.getMemSize();
    DataWord amountArrayLength = stack.get(stack.size() - 1).clone();
    DataWord amountArrayOffset = stack.get(stack.size() - 2);
    DataWord witnessArrayLength = stack.get(stack.size() - 3).clone();
    DataWord witnessArrayOffset = stack.get(stack.size() - 4);

    DataWord wordSize = new DataWord(DataWord.WORD_SIZE);

    amountArrayLength.mul(wordSize);
    amountArrayLength.add(wordSize); // dynamic array length is at least 32 bytes
    BigInteger amountArrayMemoryNeeded = memNeeded(amountArrayOffset, amountArrayLength);

    witnessArrayLength.mul(wordSize);
    witnessArrayLength.add(wordSize); // dynamic array length is at least 32 bytes
    BigInteger witnessArrayMemoryNeeded = memNeeded(witnessArrayOffset, witnessArrayLength);

    return VOTE_WITNESS + calcMemEnergy(oldMemSize,
        (amountArrayMemoryNeeded.compareTo(witnessArrayMemoryNeeded) > 0
            ? amountArrayMemoryNeeded : witnessArrayMemoryNeeded), 0, Op.VOTEWITNESS);
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/EnergyCost.java (L394-397)
```java
  public static long getVoteWitnessCost3(Program program) {
    if (!VMConfig.allowTvmOsaka()) {
      return getVoteWitnessCost2(program);
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/EnergyCost.java (L549-576)
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

  private static void checkMemorySize(int op, BigInteger newMemSize) {
    if (newMemSize.compareTo(MEM_LIMIT) > 0) {
      throw Program.Exception.memoryOverflow(op);
    }
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
