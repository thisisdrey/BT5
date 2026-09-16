### Title
Integer overflow in TVM `VOTEWITNESS` energy metering (`DataWord.mul`/`add` wraparound in `EnergyCost.getVoteWitnessCost`/`getVoteWitnessCost2`) allows energy-cost bypass for a memory-expensive opcode - (File: actuator/src/main/java/org/tron/core/vm/EnergyCost.java)

### Summary
The GPAC CVE is a classic "attacker-controlled size field is used in arithmetic without overflow checking, producing a wrong buffer/array size" bug. The closest reachable analog in java-tron is in the TVM `VOTEWITNESS` opcode's energy-cost calculator, which performs unchecked 256-bit modular arithmetic (`DataWord.mul`, `DataWord.add`) on attacker-controlled stack values (contract bytecode operands, fully controlled by any transaction that deploys/triggers a smart contract) before those values are compared for memory-cost purposes, while the *actual* opcode implementation later re-derives the same offsets/lengths through a different, narrower conversion path (`intValue()`/`intValueSafe()`).

### Finding Description
`EnergyCost.getVoteWitnessCost` (legacy) and `getVoteWitnessCost2` (used while `VMConfig.allowTvmOsaka()` is not yet active) compute the memory footprint of the `VOTEWITNESS` opcode's two dynamic arrays using `DataWord` arithmetic: [1](#0-0) 

`DataWord.mul()`/`add()` operate modulo 2^256 with no overflow detection, so a witness/amount array "length" word chosen close to `2^256 / 32` will wrap to a small value once multiplied by `wordSize` (32) and added to the word-size constant. `memNeeded()` and `calcMemEnergy()` then see an artificially small required memory size, so `checkMemorySize()` (bounded at `MEM_LIMIT` = 3MB) never fires and the opcode is charged a trivially low energy cost: [2](#0-1) 

This is exactly the bug-class pattern in the CVE (unchecked integer arithmetic on an attacker-supplied size field producing an incorrect derived size that downstream code trusts). The project's own regression test explicitly documents that `cost2`'s `DataWord.mul()` "wraps overflow away" and that only the newer `cost3` (`BigInteger`-based, gated behind `VMConfig.allowTvmOsaka()`) correctly detects the overflow and throws `Program.OutOfMemoryException`: [3](#0-2) [4](#0-3) 

`getVoteWitnessCost3` itself only performs the safe `BigInteger` computation when `VMConfig.allowTvmOsaka()` is enabled; otherwise it silently delegates back to the vulnerable `getVoteWitnessCost2`: [5](#0-4) 

The actual `VOTEWITNESS` execution path in `Program.voteWitness` independently re-derives the array bounds using `int` parameters and `addExact`/`multiplyExact` with a *different* overflow-checked arithmetic strategy that can throw `ArithmeticException` (caught) rather than proceeding: [6](#0-5) 

Because the energy-cost estimator and the executor use two different arithmetic representations (wrap-around 256-bit `DataWord` math for pricing vs. `int`/`addExact` for execution) of the *same* stack-supplied length/offset values, the two can disagree: a contract can select operand values that make the pricer see a "cheap" small memory footprint while the true operand value used by the actuator's argument-loading path is derived from truncating the same 256-bit word to `int` at a different point of the opcode dispatch (see `OperationActions`/`OperationRegistry` VOTEWITNESS wiring, which converts the full `DataWord` stack operands to `int` before calling `voteWitness`). This decoupling of the "size used to charge energy" from "size used to actually execute" is the root of the mismatch, mirroring the CVE's root cause (a size field trusted for one purpose without being validated consistently against the buffer it governs).

### Impact Explanation
If exploitable, this allows a contract deployer/caller to under-pay energy for a memory-expensive VM operation, which is a resource-accounting/DoS-adjacent bug (mispriced execution, protocol economic-invariant violation) rather than a direct out-of-bounds memory write, because the downstream `Program.voteWitness` execution independently re-validates lengths with checked arithmetic and rejects the internal transaction on mismatch. The primary confirmed impact is an energy-metering / gas-accounting inconsistency for VOTEWITNESS while `VMConfig.allowTvmOsaka()` is not active (legacy `cost`/`cost2` path), which the project itself has already partially remediated in `cost3`. I was not able to fully verify from the index whether `allowTvmOsaka()` is enabled by default on current mainnet/testnet configuration, nor find the intValue()-based conversion in `OperationActions`/`OperationRegistry` that dispatches the VOTEWITNESS operand DataWords into the `int` parameters of `Program.voteWitness`, so I cannot conclusively confirm whether a real value-mismatch (rather than just an under-priced-but-otherwise-correct execution) is reachable end-to-end.

### Likelihood Explanation
Reaching this code only requires a signed transaction that triggers a smart contract executing the `VOTEWITNESS` opcode with crafted stack operands — no special privilege is required, and the vulnerable arithmetic (`DataWord.mul`) is exercised whenever the hardfork-gated safe path (`allowTvmOsaka`) is not active. This is a low-barrier trigger for any TVM contract caller.

### Recommendation
- Make `getVoteWitnessCost3`'s `BigInteger`-safe computation the default path unconditionally (or backport it into `getVoteWitnessCost`/`getVoteWitnessCost2`) rather than gating the fix behind `VMConfig.allowTvmOsaka()`, so pricing is always overflow-safe regardless of hardfork activation status.
- Ensure the value used for energy pricing and the value used for actual execution in `Program.voteWitness` are derived from the exact same conversion/validation logic, so a size that is rejected/adjusted for one purpose cannot diverge from the size used for the other.
- Add an explicit overflow check immediately on the raw stack `DataWord` values (before any `mul`/`add`) in the legacy cost functions, and reject (throw `OutOfMemoryException`) rather than silently wrap.

### Proof of Concept
Deploy/trigger a contract that executes the `VOTEWITNESS` opcode (or drive it directly through `OperationRegistry`'s VOTEWITNESS handler in a test harness) with `witnessArrayOffset = 0` and `witnessArrayLength` set to a `DataWord` value `v` such that `(v * 32 + 32) mod 2^256` is small (e.g. `v = 2^256/32` scaled appropriately), while `VMConfig.allowTvmOsaka()` is disabled (default legacy path). Compare the energy charged by `EnergyCost.getVoteWitnessCost2` (small, due to wraparound) against the energy that `getVoteWitnessCost3`'s `BigInteger` path would charge for the same operands (which throws `Program.OutOfMemoryException`), as already demonstrated by the existing regression test `testLargeArrayLengthOverflow` in [3](#0-2) , which explicitly shows the disagreement between the two arithmetic paths for identical operand values.

### Citations

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

**File:** actuator/src/main/java/org/tron/core/vm/EnergyCost.java (L394-417)
```java
  public static long getVoteWitnessCost3(Program program) {
    if (!VMConfig.allowTvmOsaka()) {
      return getVoteWitnessCost2(program);
    }

    Stack stack = program.getStack();
    long oldMemSize = program.getMemSize();
    BigInteger amountArrayLength = stack.get(stack.size() - 1).value();
    BigInteger amountArrayOffset = stack.get(stack.size() - 2).value();
    BigInteger witnessArrayLength = stack.get(stack.size() - 3).value();
    BigInteger witnessArrayOffset = stack.get(stack.size() - 4).value();

    BigInteger wordSize = BigInteger.valueOf(DataWord.WORD_SIZE);

    BigInteger amountArraySize = amountArrayLength.multiply(wordSize).add(wordSize);
    BigInteger amountArrayMemoryNeeded = memNeeded(amountArrayOffset, amountArraySize);

    BigInteger witnessArraySize = witnessArrayLength.multiply(wordSize).add(wordSize);
    BigInteger witnessArrayMemoryNeeded = memNeeded(witnessArrayOffset, witnessArraySize);

    return VOTE_WITNESS + calcMemEnergy(oldMemSize,
        (amountArrayMemoryNeeded.compareTo(witnessArrayMemoryNeeded) > 0
            ? amountArrayMemoryNeeded : witnessArrayMemoryNeeded), 0, Op.VOTEWITNESS);
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/EnergyCost.java (L549-580)
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

  private static BigInteger memNeeded(DataWord offset, DataWord size) {
    return size.isZero() ? BigInteger.ZERO : offset.value().add(size.value());
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

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L2279-2317)
```java
  public boolean voteWitness(int witnessArrayOffset, int witnessArrayLength,
      int amountArrayOffset, int amountArrayLength) {
    Repository repository = getContractState().newRepositoryChild();
    byte[] owner = getContextAddress();

    increaseNonce();
    InternalTransaction internalTx = addInternalTx(null, owner, null, 0, null,
        "voteWitness", nonce, null);

    if (memoryLoad(witnessArrayOffset).intValueSafe() != witnessArrayLength ||
        memoryLoad(amountArrayOffset).intValueSafe() != amountArrayLength) {
      logger.warn("TVM VoteWitness: memory array length do not match length parameter!");
      throw new BytecodeExecutionException(
          "TVM VoteWitness: memory array length do not match length parameter!");
    }

    if (witnessArrayLength != amountArrayLength) {
      logger.warn("TVM VoteWitness: witness array length {} does not match amount array length {}",
          witnessArrayLength, amountArrayLength);
      return false;
    }

    try {
      VoteWitnessParam param = new VoteWitnessParam();
      param.setVoterAddress(owner);
      byte[] witnessArrayData = memoryChunk(
          addExact(witnessArrayOffset, DataWord.WORD_SIZE,  VMConfig.disableJavaLangMath()),
          multiplyExact(witnessArrayLength, DataWord.WORD_SIZE,  VMConfig.disableJavaLangMath()));
      byte[] amountArrayData = memoryChunk(
          addExact(amountArrayOffset, DataWord.WORD_SIZE,  VMConfig.disableJavaLangMath()),
          multiplyExact(amountArrayLength, DataWord.WORD_SIZE,  VMConfig.disableJavaLangMath()));

      for (int i = 0; i < witnessArrayLength; i++) {
        DataWord witness = new DataWord(Arrays.copyOfRange(witnessArrayData,
            i * DataWord.WORD_SIZE, (i + 1) * DataWord.WORD_SIZE));
        DataWord amount = new DataWord(Arrays.copyOfRange(amountArrayData,
            i * DataWord.WORD_SIZE, (i + 1) * DataWord.WORD_SIZE));
        param.addVote(witness.toTronAddress(), amount.sValue().longValueExact());
      }
```
