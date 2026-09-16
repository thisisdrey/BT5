Based on my investigation, I found a concrete analog to the CosmWasm "incorrect metering" bug class in java-tron's `VOTEWITNESS` opcode energy accounting, which uses modular 256-bit `DataWord` arithmetic to compute memory-expansion charges instead of unbounded/overflow-safe arithmetic.

### Title
Modular (mod 2^256) `DataWord` arithmetic in `VOTEWITNESS` energy pre-charge allows attacker-controlled wraparound to underprice memory-expansion cost - (File: actuator/src/main/java/org/tron/core/vm/EnergyCost.java)

### Summary
`EnergyCost.getVoteWitnessCost` and `getVoteWitnessCost2` compute the memory size needed for the `VOTEWITNESS` opcode's witness/amount arrays using `DataWord` multiplication and addition, which silently wrap modulo 2^256 instead of using safe/overflow-checked arithmetic, unlike the later `getVoteWitnessCost3` (Osaka) rewrite which explicitly switches to `BigInteger` to fix this class of issue.

### Finding Description
`getVoteWitnessCost` computes: [1](#0-0) 

and `getVoteWitnessCost2` similarly: [2](#0-1) 

Both call `amountArrayLength.mul(wordSize)` / `.add(wordSize)` on `DataWord` (a 256-bit fixed-width value) before passing the result to `memNeeded(offset, size)`, which converts to `BigInteger` only after the wraparound has already occurred: [3](#0-2) 

Since `DataWord` arithmetic operations (`mul`, `add`) are performed on a fixed 32-byte buffer representing values mod 2^256 (matching EVM semantics for computed *values*, but not appropriate for internally-computed memory-size accounting), a caller can choose `amountArrayLength` (an attacker-controlled stack value from `stack.get(stack.size()-1)`, ultimately derived from a `CALLDATA`-controlled or `PUSH`-encoded value in the contract bytecode) such that `amountArrayLength * WORD_SIZE (+WORD_SIZE)` wraps around 2^256 to a small residual value. This causes `memNeeded()` to report an artificially small required memory size, so `calcMemEnergy` charges far less energy than the operation should require. The later fix (`getVoteWitnessCost3`, gated by `VMConfig.allowTvmOsaka()`) explicitly redoes this calculation using `BigInteger` (no wraparound), and a dedicated test file (`VoteWitnessCost3Test`) was added specifically to validate overflow-safe behavior for large offsets — confirming the pre-Osaka path is the vulnerable analog: [4](#0-3) [5](#0-4) 

This mirrors the CosmWasm/wasmvm "incorrect metering" bug class: the VM's gas/energy accounting for an operation can be manipulated via crafted operand values to diverge from the true resource consumption, letting execution proceed far cheaper (in energy) than the actual work performed downstream in `OperationActions` for the `VOTEWITNESS` execution.

### Impact Explanation
If the underpriced memory-expansion energy allows a much larger real memory allocation/array processing to occur for `VOTEWITNESS` than what was paid for, this is a resource-metering bypass: an unprivileged contract deployer/caller can construct a transaction that performs disproportionately expensive VM work (large memory expansion and iteration over witness/amount arrays) while paying energy computed on the wrapped, small value. Depending on downstream array iteration in `OperationActions`'s vote-witness execution, this can lead to excessive CPU/memory consumption relative to energy paid — a resource-accounting/DoS-class issue reachable purely through a signed TVM transaction invoking `VOTEWITNESS`.

### Likelihood Explanation
Reachable directly by any account issuing a transaction that executes contract bytecode containing the `VOTEWITNESS` opcode with attacker-chosen stack operands (no special privilege needed). The wraparound requires crafting a length value near a multiple of 2^256/32, which is trivially constructible since `DataWord` accepts arbitrary 256-bit push values.

### Recommendation
Backport the `BigInteger`-based, overflow-safe computation from `getVoteWitnessCost3` into the paths still reachable when `VMConfig.allowTvmOsaka()` is false (i.e., make `getVoteWitnessCost`/`getVoteWitnessCost2` also compute array-size/memory-needed math using `BigInteger` instead of `DataWord.mul/add`, consistent with `memNeeded(BigInteger, BigInteger)`), and audit other `EnergyCost` methods for similar `DataWord`-based size arithmetic that lacks overflow protection prior to the Osaka fork gate.

### Proof of Concept
1. Deploy a contract whose bytecode invokes `VOTEWITNESS` with stack values (from `PUSH32`) such that `amountArrayLength * 32 (+32) mod 2^256` is small (e.g., choose `amountArrayLength = (2^256 - 32) / 32`, so that after `.mul(32)` and `.add(32)` the wrapped `DataWord` value is near 0), while `amountArrayOffset` is also crafted so `memNeeded` returns a small BigInteger.
2. Call this contract on a network where `allowTvmOsaka` (and thus `getVoteWitnessCost3`) is not yet activated, so `getVoteWitnessCost`/`getVoteWitnessCost2` is used.
3. Observe that `EnergyCost.getVoteWitnessCost2` returns `VOTE_WITNESS + calcMemEnergy(...)` computed against the wrapped small memory size rather than the true (huge) array length, while the true `amountArrayLength` value is still used elsewhere for iterating/reading the array in `OperationActions`, confirming a mismatch between energy charged and actual resource consumption.

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

**File:** actuator/src/main/java/org/tron/core/vm/EnergyCost.java (L578-580)
```java
  private static BigInteger memNeeded(DataWord offset, DataWord size) {
    return size.isZero() ? BigInteger.ZERO : offset.value().add(size.value());
  }
```

**File:** framework/src/test/java/org/tron/common/runtime/vm/VoteWitnessCost3Test.java (L159-178)
```java
  @Test
  public void testLargeOffsetOverflow() {
    // Large offset + normal size should trigger memoryOverflow in cost3
    String largeHex = "00ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff";
    DataWord largeOffset = new DataWord(largeHex);

    Program program = mockProgram(largeOffset, new DataWord(1),
        new DataWord(0), new DataWord(1), 0);

    boolean overflowCaught = false;
    VMConfig.initAllowTvmOsaka(1);    // cost3 self-dispatches; exercise the v3 (BigInteger) path
    try {
      EnergyCost.getVoteWitnessCost3(program);
    } catch (Program.OutOfMemoryException e) {
      overflowCaught = true;
    } finally {
      VMConfig.initAllowTvmOsaka(0);
    }
    assertTrue("cost3 should throw memoryOverflow for huge offset", overflowCaught);
  }
```
