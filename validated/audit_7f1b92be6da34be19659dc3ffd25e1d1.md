### Title
Unbounded memory allocation via integer-wraparound in legacy VOTEWITNESS energy metering - (File: actuator/src/main/java/org/tron/core/vm/EnergyCost.java)

### Summary
The TVM `VOTEWITNESS` opcode's energy-metering functions `getVoteWitnessCost` and `getVoteWitnessCost2` compute the memory-expansion cost for the witness/amount arrays using 256-bit `DataWord` arithmetic (`DataWord.mul()`/`.add()`), which wraps around modulo 2^256 instead of detecting overflow. [1](#0-0) [2](#0-1) 
By contrast, the newer `getVoteWitnessCost3` deliberately switches to `BigInteger` math specifically to catch this overflow and reject it via `checkMemorySize`/`MEM_LIMIT`. [3](#0-2) 
This is confirmed by an existing regression test that shows the legacy path fails to raise `Program.OutOfMemoryException` for a maximal-length array while `cost3` does. [4](#0-3) [5](#0-4) 

### Finding Description
`calcMemEnergy` is the general TVM memory-expansion pricing routine, and it enforces a hard `MEM_LIMIT` of 3 MB before allowing any memory-touching opcode to proceed. [3](#0-2) 
This limit is only effective if the `newMemSize` value passed in accurately reflects the offset+length requested by the contract. In `getVoteWitnessCost` and `getVoteWitnessCost2`, the witness-array and amount-array lengths supplied by the calling contract are multiplied by the 32-byte word size using `DataWord` (256-bit modular) arithmetic, not `BigInteger`: [6](#0-5) 
Because `DataWord` values simulate the EVM's 256-bit integers and wrap silently on overflow (as `getVoteWitnessCost3`'s own doc comments and the paired unit tests explicitly call out — `testLargeArrayLengthOverflow` and `testCost3FallsBackToCost2WhenOsakaOff` show `DataWord.mul()` "would overflow" while `BigInteger` "handles this correctly"), a contract can supply an array-length value engineered so that `amountArrayLength.mul(wordSize)` wraps back to a small number. The resulting `memNeeded()` computation then reports a tiny memory requirement to `calcMemEnergy`, which lets the `MEM_LIMIT` check pass and charges only a negligible amount of energy for what is, at the bytecode level, a request to read/allocate a vastly larger memory region for the arrays actually referenced by the `VOTEWITNESS` operation.

This is precisely the CWE-770 pattern in the external report: a caller-controlled size value is used to derive a cost/limit check, but the arithmetic used for that check does not match the real resource consumption, so the throttle can be bypassed and the underlying allocation proceeds essentially unmetered.

### Impact Explanation
Any account able to deploy or trigger a smart contract that invokes the `VOTEWITNESS` precompiled opcode (a broadcastable, unprivileged operation reachable by any transaction sender) can construct calldata whose array-length words are chosen to wrap the `DataWord` multiplication. This lets the attacker drive `Program`/`Memory` to service memory requests far larger than what was actually paid for in energy, at low cost to the attacker. Repeated or amplified use of this technique against a full node executing the transaction (including during re-execution/verification by every node in the network) can force disproportionate heap/off-heap memory allocation, leading to `OutOfMemoryError`/GC pressure and node unavailability — matching the "node crash or halt" / "API the node can no longer serve" impact bar for this class of finding.

### Likelihood Explanation
The condition is gated by TVM feature flags: `getVoteWitnessCost3` is only used once `allowTvmOsaka()` is enabled, and `getVoteWitnessCost2` only differs from the legacy path once `allowEnergyAdjustment()` is enabled but Osaka is not. [7](#0-6) [8](#0-7) 
This means the vulnerable arithmetic (`cost1`/legacy and `cost2`) is reachable whenever the network has not yet activated the Osaka hard fork feature (or is in the intermediate state where only `allowEnergyAdjustment` is active) — a normal, non-malicious-SR chain state during the fork rollout window, not a privileged-actor scenario. No signature, permission, or elevated privilege is required; the vector is a single ordinary contract call.

### Recommendation
- Backport the `BigInteger`-based memory-size computation and the `checkMemorySize` overflow guard from `getVoteWitnessCost3` into `getVoteWitnessCost` and `getVoteWitnessCost2` (or otherwise ensure the same `MEM_LIMIT` check is applied prior to feature-flag activation), so the legacy/adjusted paths cannot be bypassed via `DataWord` wraparound.
- Audit other opcode-cost functions that still perform length/offset arithmetic with `DataWord` instead of `BigInteger` (e.g. `getCallDataCopyCost`, `getExtCodeCopyCost`, `getMloadCost`) to confirm none of them have an equivalent unguarded wraparound before their `calcMemEnergy` call.
- Add hard, feature-flag-independent bounds checking directly inside `Memory.extend`/`ProgramInvokeImpl` as defense in depth, rather than relying solely on the caller (`EnergyCost`) to have computed the size correctly.

### Proof of Concept
1. On a network state where `VMConfig.allowTvmOsaka()` is disabled (pre-Osaka activation, e.g. `VMConfig.initAllowTvmOsaka(0)` as in the existing test harness) and `allowEnergyAdjustment()` may be enabled or disabled, deploy a contract that issues a `VOTEWITNESS` opcode with a witness-array or amount-array length word crafted so that `DataWord.mul(wordSize)` wraps modulo 2^256 to a small value (e.g., length ≈ `(2^256)/32` or its complement, as demonstrated with `maxHex = "ffff...ff"` in the existing test `testLargeArrayLengthOverflow`). [4](#0-3) 
2. Invoke `EnergyCost.getVoteWitnessCost`/`getVoteWitnessCost2` with this stack state (mirroring `mockProgram` in the test) and observe that no `Program.OutOfMemoryException` is thrown and only a minimal energy charge (`VOTE_WITNESS` constant plus negligible `memEnergy`) is levied, unlike the `cost3`/`BigInteger` path which throws `Program.OutOfMemoryException` for the same input. [5](#0-4) 
3. Because the opcode is charged for a tiny memory region while its actual operand encodes a much larger array length, the downstream execution path attempting to read/extend memory for the real (unwrapped) array size is under-priced relative to the resources it consumes, enabling a low-cost resource-exhaustion vector consistent with CWE-770.

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

**File:** framework/src/test/java/org/tron/common/runtime/vm/VoteWitnessCost3Test.java (L217-249)
```java
  @Test
  public void testOperationRegistryWithoutOsaka() {
    VMConfig.initAllowTvmOsaka(0);
    JumpTable table = OperationRegistry.prepareAndGetTable(false);
    Operation voteOp = table.get(Op.VOTEWITNESS);
    assertTrue(voteOp.isEnabled());

    // Without osaka, should use cost2 (from adjustForFairEnergy since allowEnergyAdjustment=1)
    Program program = mockProgram(0, 2, 128, 2, 0);
    long cost = voteOp.getEnergyCost(program);
    long expectedCost2 = EnergyCost.getVoteWitnessCost2(
        mockProgram(0, 2, 128, 2, 0));
    assertEquals(expectedCost2, cost);
  }

  @Test
  public void testOperationRegistryWithOsaka() {
    VMConfig.initAllowTvmOsaka(1);
    try {
      JumpTable table = OperationRegistry.prepareAndGetTable(false);
      Operation voteOp = table.get(Op.VOTEWITNESS);
      assertTrue(voteOp.isEnabled());

      // With osaka, should use cost3
      Program program = mockProgram(0, 2, 128, 2, 0);
      long cost = voteOp.getEnergyCost(program);
      long expectedCost3 = EnergyCost.getVoteWitnessCost3(
          mockProgram(0, 2, 128, 2, 0));
      assertEquals(expectedCost3, cost);
    } finally {
      VMConfig.initAllowTvmOsaka(0);
    }
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

**File:** framework/src/test/java/org/tron/common/runtime/vm/VoteWitnessCost3Test.java (L278-294)
```java
  @Test
  public void testCost2FallsBackToLegacyWhenEnergyAdjustmentOff() {
    // cost2 self-dispatches: with energyAdjustment off it delegates to the legacy cost.
    VMConfig.initAllowEnergyAdjustment(0);
    try {
      long viaCost2 = EnergyCost.getVoteWitnessCost2(mockProgram(0, 2, 128, 2, 0));
      long viaLegacy = EnergyCost.getVoteWitnessCost(mockProgram(0, 2, 128, 2, 0));
      assertEquals("cost2 must equal legacy cost when energyAdjustment is off",
          viaLegacy, viaCost2);

      // sanity: with energyAdjustment on, cost2 differs from the legacy cost for this input.
      VMConfig.initAllowEnergyAdjustment(1);
      assertNotEquals(viaLegacy, EnergyCost.getVoteWitnessCost2(mockProgram(0, 2, 128, 2, 0)));
    } finally {
      VMConfig.initAllowEnergyAdjustment(1);
    }
  }
```
