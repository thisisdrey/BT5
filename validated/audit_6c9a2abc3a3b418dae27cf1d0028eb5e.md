[1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/EnergyCost.java (L346-364)
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
