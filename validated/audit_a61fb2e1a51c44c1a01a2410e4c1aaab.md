### Title
Integer overflow in `EnergyCost.getVoteWitnessCost`/`getVoteWitnessCost2` bypasses the `MEM_LIMIT` sanity check, letting an attacker cheaply trigger an uncaught out-of-memory crash in `Program.voteWitness` - ([File: actuator/src/main/java/org/tron/core/vm/EnergyCost.java])

### Summary
`EnergyCost.getVoteWitnessCost`/`getVoteWitnessCost2` compute the memory size needed for the `VOTEWITNESS` TVM opcode using 256-bit modular arithmetic (`DataWord.mul()`), which silently wraps around instead of overflowing safely. This lets an attacker craft a `witnessArrayLength`/`amountArrayLength` value that appears small to the `MEM_LIMIT` sanity check in `checkMemorySize()`, while the actual opcode handler (`Program.voteWitness`) receives that same length converted through a different, non-wrapping path (`intValueSafe()`, clamped to `Integer.MAX_VALUE`) and attempts to allocate/read tens of gigabytes of VM memory, producing an uncaught `OutOfMemoryError`.

### Finding Description
`EnergyCost.getVoteWitnessCost2` multiplies the attacker-controlled 256-bit stack values by the word size using `DataWord.mul()`: [1](#0-0) 

`DataWord.mul()` computes `value().multiply(word.value())` and then masks the result with `MAX_VALUE` (`result.and(MAX_VALUE)`), i.e. it performs modulo-2²⁵⁶ arithmetic with no overflow detection: [2](#0-1) 

The resulting (possibly wrapped-around, small) `amountArrayMemoryNeeded`/`witnessArrayMemoryNeeded` is fed into `calcMemEnergy` → `checkMemorySize`, whose entire purpose is to reject requests that exceed `MEM_LIMIT`: [3](#0-2) 

By choosing a `witnessArrayLength` such that `witnessArrayLength * 32 mod 2^256` is small, an attacker makes this size check pass and pays a tiny amount of energy for what is, in reality, an enormous array length.

The actual opcode execution in `Program.voteWitness` receives `witnessArrayLength` as a plain `int` (already converted independently from the same 256-bit stack value, via `intValueSafe()`, which does **not** wrap—​it clamps to `Integer.MAX_VALUE` for any value occupying more than 4 bytes). It then performs `multiplyExact(witnessArrayLength, WORD_SIZE, ...)` (safe long arithmetic, does not throw for values this size) and passes the result to `memoryChunk`/`Memory.extend`, which tries to allocate the full requested memory (tens of gigabytes): [4](#0-3) 

The only exceptions caught around this logic are `ContractValidateException`, `ContractExeException`, and `ArithmeticException`: [5](#0-4) 

`OutOfMemoryError` is not an `Exception` and is not caught here (or, as far as could be verified, anywhere up the TVM execution call chain for opcode handlers), so it propagates as an uncaught JVM `Error`. Because this same transaction is executed deterministically by every full node and SR that validates or applies the block containing it, all nodes hit the same allocation and OOM condition simultaneously.

This mirrors the CVE-2026-43907 bug class exactly: a signed/wrapping integer overflow in a size-validation routine (`QueryRGBBufferSizeInternal` / here `getVoteWitnessCost2` + `checkMemorySize`) is used as an in-band "this is safe" signal, while a separate downstream computation (the DPX `fread`/`m_decodebuf.resize` call, here `Program.voteWitness`'s `memoryChunk`/`Memory.extend`) uses the real, unwrapped magnitude of the value and performs the oversized operation anyway.

### Impact Explanation
An attacker-controlled contract can invoke the `VOTEWITNESS` opcode (reachable from any deployed contract via inline bytecode, triggerable by any unprivileged account through a normal `TriggerSmartContract`) with a crafted witness/amount array length that wraps `DataWord.mul()` to a tiny value. This lets the transaction pass the `MEM_LIMIT` overflow guard almost for free, then causes the JVM to attempt an enormous (multi-gigabyte) memory allocation inside `Memory.extend`, which is very likely to raise an uncaught `OutOfMemoryError` during block validation/application. Because block execution is deterministic, this crashes every node that processes the transaction, which can halt the network / cause a consensus-affecting node crash — matching the "node crash or halt" acceptance criterion.

### Likelihood Explanation
Reachability is straightforward: any account can deploy a small contract that executes the `VOTEWITNESS` opcode with carefully chosen offset/length words and broadcast a single transaction calling it, without any special privilege. The only obstacle is crafting a `witnessArrayLength` value whose `*32 mod 2^256` product is small enough to pass `MEM_LIMIT` while the clamped `intValueSafe()` int conversion used at execution time is still large (e.g., any value with more than 4 significant bytes automatically clamps to `Integer.MAX_VALUE`, which is trivial to satisfy simultaneously with a wrap condition on the 256-bit multiply). This does not require SR/witness/committee privilege, only an ordinary funded account able to pay the (deliberately mispriced) low energy cost.

### Recommendation
Compute the VOTEWITNESS memory-size requirement using non-wrapping arithmetic consistent with the `MEM_LIMIT` check — this is effectively what `getVoteWitnessCost3` already does with `BigInteger` (gated behind `VMConfig.allowTvmOsaka()`). The overflow-safe path should be made the default (not gated behind a future hard fork flag) or backported, and `Program.voteWitness`/`Memory.extend` should also guard against extremely large allocations (e.g., enforce `MEM_LIMIT` again at allocation time and catch `OutOfMemoryError`/`Throwable` around opcode execution) rather than relying solely on the (overflow-vulnerable) upstream energy-cost check.

### Proof of Concept
1. Deploy a contract containing raw bytecode that pushes onto the stack a `witnessArrayLength` value `L` such that `L * 32 mod 2^256` is small (e.g., choose `L = (2^256 / 32) + k` for small `k`, so the product wraps near zero), together with a matching `amountArrayLength` and offsets pointing at attacker-controlled memory that has been `MSTORE`'d to satisfy the `memoryLoad(...).intValueSafe() == witnessArrayLength` check in `Program.voteWitness` (note `intValueSafe()` clamps any value with `bytesOccupied() > 4` to `Integer.MAX_VALUE`, so simply storing `Integer.MAX_VALUE` at the offset satisfies this check for any qualifying `L`).
2. Invoke the `VOTEWITNESS` opcode with these stack values.
3. `EnergyCost.getVoteWitnessCost2` computes a small `memNeeded` due to `DataWord.mul()` wraparound, passes `checkMemorySize`, and charges minimal energy.
4. `Program.voteWitness` converts the same stack value via `intValueSafe()` (clamped to `Integer.MAX_VALUE`), computes `multiplyExact(Integer.MAX_VALUE, 32)` (≈6.8×10¹⁰, fits in `long`, no `ArithmeticException`), and calls `memoryChunk`/`Memory.extend`, which attempts to allocate roughly 64 GB of heap via chunked `byte[1024]` allocations, triggering an uncaught `OutOfMemoryError` that is not handled by the surrounding `catch (ContractValidateException | ContractExeException | ArithmeticException)` block.
5. Broadcast the transaction; every full node/SR that validates or applies the containing block experiences the same OOM condition.

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

**File:** common/src/main/java/org/tron/common/runtime/vm/DataWord.java (L344-350)
```java
  // TODO: mul can be done in more efficient way
  // TODO:     with shift left shift right trick
  // TODO      without BigInteger quick hack
  public void mul(DataWord word) {
    BigInteger result = value().multiply(word.value());
    this.data = ByteUtil.copyToArray(result.and(MAX_VALUE));
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

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L2327-2337)
```java
    } catch (ContractValidateException e) {
      logger.warn("TVM VoteWitness: validate failure. Reason: {}", e.getMessage());
    } catch (ContractExeException e) {
      logger.warn("TVM VoteWitness: execute failure. Reason: {}", e.getMessage());
    } catch (ArithmeticException e) {
      logger.warn("TVM VoteWitness: int or long out of range. caused by: {}", e.getMessage());
    }
    if (internalTx != null) {
      internalTx.reject();
    }
    return false;
```
