### Title
Unchecked `DataWord.intValue()`/`longValue()` casting silently truncates 256-bit stack values used as memory offsets/sizes and TRC10 token IDs in TVM calls - (File: `common/src/main/java/org/tron/common/runtime/vm/DataWord.java`)

### Summary
`DataWord.intValue()` and `DataWord.longValue()` are documented to throw `ArithmeticException` when the underlying 256-bit word doesn't fit into an `int`/`long`, but their implementations contain no range check at all — they simply accumulate all 32 bytes of the word via repeated left shifts, silently discarding the high-order bits [1](#0-0) [2](#0-1) . This is the exact "casting overflow" bug class from the report: a large value is silently coerced into a smaller type instead of reverting, and downstream code trusts the truncated value.

### Finding Description
The codebase itself acknowledges the danger of unchecked truncation by providing "safe" variants — `intValueSafe()` and `longValueSafe()` — which explicitly check `bytesOccupied()` and clamp to `Integer.MAX_VALUE`/`Long.MAX_VALUE` instead of wrapping [3](#0-2) [4](#0-3) . However, several fund-relevant call sites in `Program.java` still use the unsafe `intValue()`/`longValue()` directly on attacker-controlled stack words that originate from a smart-contract's `CALL`/`CALLCODE`/`DELEGATECALL` opcode arguments (any unprivileged contract-calling transaction can push arbitrary 256-bit values onto the TVM stack):

- Call input memory offset/size are read with unchecked `intValue()`: `memoryChunk(msg.getInDataOffs().intValue(), msg.getInDataSize().intValue())` [5](#0-4) .
- Call output memory offset/size are likewise read with unchecked `intValue()` when writing the callee's return data back into memory: `int offset = msg.getOutDataOffs().intValue(); int size = msg.getOutDataSize().intValue(); memorySaveLimited(offset, buffer, size);` [6](#0-5) .
- TRC10 token id used to select which asset balance to debit/credit for a token-transfer call is derived with unchecked `longValue()`: `tokenId = String.valueOf(msg.getTokenId().longValue()).getBytes();` [7](#0-6)  and again in the precompiled-contract call path [8](#0-7) .

By contrast, other parts of the same file are careful to use `longValueExact()`/`intValueSafe()` for value-bearing fields such as `endowment` (e.g. `msg.getEndowment().value().longValueExact()`, wrapped in a try/catch for `ArithmeticException`) [9](#0-8) , and `callToPrecompiledAddress` even switches to `intValueSafe()` specifically to avoid this exact truncation hazard when a feature flag is enabled [10](#0-9)  — confirming the team is aware that raw `intValue()` is unsafe for these code paths, yet several sibling call sites (lines 1019, 1199-1200, 1069, 1701) were never migrated to the safe variants.

### Impact Explanation
Because `intValue()`/`longValue()` neither throw nor clamp, any contract invoked in a transaction can supply out-data-offset/size or token-id stack words whose low 32/64 bits wrap to an arbitrary (including negative) value while their true magnitude is astronomically large. This silent truncation, rather than a controlled revert, means:
- Memory offset/size values passed to `memorySaveLimited`/`memoryChunk` can become negative or otherwise inconsistent with the actual (unbounded) value the contract intended, leading to unpredictable read/write behavior in `Memory`, which is charged/validated elsewhere assuming `intValueSafe()`-style values.
- A token id can silently wrap into an unrelated, smaller TRC10 asset id, causing the TVM to operate on the wrong asset accounting path for that call.

This is the same class of defect described in the reference report: values that should be safely capped or rejected are instead corrupted by a lossy cast, and the corrupted value is then trusted by balance/memory-affecting logic within the TVM execution path reachable by any account submitting a contract-calling transaction.

### Likelihood Explanation
Reaching this code only requires deploying/calling a smart contract that issues `CALL`/`CALLCODE`/`DELEGATECALL`/token-transfer opcodes with crafted (huge) offset/size/token-id operands — something any unprivileged contract deployer/caller can do at will, with no special permissions. The absence of any exception or clamping in `intValue()`/`longValue()`, contradicting their own Javadoc contract, makes this trivially and repeatedly triggerable.

### Recommendation
Replace the unsafe `intValue()`/`longValue()` calls at `Program.java:1019`, `Program.java:1199-1200`, `Program.java:1069`, and `Program.java:1701` with the existing `intValueSafe()`/`longValueSafe()` (or `longValueExact()`/`intValueExact()`-style validation that throws) so oversized 256-bit stack values are rejected or safely clamped rather than silently truncated, consistent with how `endowment` and the `allowTvmSelfdestructRestriction`-gated path already handle this. Additionally, fix `DataWord.intValue()`/`longValue()` to match their documented contract (throw `ArithmeticException` on loss of information) or rename/deprecate them to avoid future unsafe use.

### Proof of Concept
1. Deploy a contract `Attacker` that executes a low-level `call` (or `callcode`/`delegatecall`) where the `retSize` (or `retOffset`) argument is set to a 256-bit value such as `2^32 + 4` (i.e., a value whose low 32 bits are small/negative but whose true magnitude exceeds `Integer.MAX_VALUE`).
2. Submit a transaction invoking `Attacker`, causing TVM execution to enter `Program.callToAddress` and reach `int size = msg.getOutDataSize().intValue();` [11](#0-10) .
3. Because `intValue()` performs unchecked repeated left-shift accumulation over the full 32-byte word [12](#0-11) , `size` silently becomes a small/negative int instead of triggering the energy/memory bounds validation that assumes safe/clamped values, so `memorySaveLimited(offset, buffer, size)` operates on a value inconsistent with what the caller actually specified on the stack — diverging from the intended (and separately energy-charged) memory semantics used elsewhere in the same call.

### Citations

**File:** common/src/main/java/org/tron/common/runtime/vm/DataWord.java (L202-217)
```java
  /**
   * Converts this DataWord to an int, checking for lost information. If this DataWord is out of the
   * possible range for an int result then an ArithmeticException is thrown.
   *
   * @return this DataWord converted to an int.
   * @throws ArithmeticException - if this will not fit in an int.
   */
  public int intValue() {
    int intVal = 0;

    for (byte aData : data) {
      intVal = (intVal << 8) + (aData & 0xff);
    }

    return intVal;
  }
```

**File:** common/src/main/java/org/tron/common/runtime/vm/DataWord.java (L219-229)
```java
  /**
   * In case of int overflow returns Integer.MAX_VALUE otherwise works as #intValue()
   */
  public int intValueSafe() {
    int bytesOccupied = bytesOccupied();
    int intValue = intValue();
    if (bytesOccupied > 4 || intValue < 0) {
      return Integer.MAX_VALUE;
    }
    return intValue;
  }
```

**File:** common/src/main/java/org/tron/common/runtime/vm/DataWord.java (L231-246)
```java
  /**
   * Converts this DataWord to a long, checking for lost information. If this DataWord is out of the
   * possible range for a long result then an ArithmeticException is thrown.
   *
   * @return this DataWord converted to a long.
   * @throws ArithmeticException - if this will not fit in a long.
   */
  public long longValue() {

    long longVal = 0;
    for (byte aData : data) {
      longVal = (longVal << 8) + (aData & 0xff);
    }

    return longVal;
  }
```

**File:** common/src/main/java/org/tron/common/runtime/vm/DataWord.java (L248-258)
```java
  /**
   * In case of long overflow returns Long.MAX_VALUE otherwise works as #longValue()
   */
  public long longValueSafe() {
    int bytesOccupied = bytesOccupied();
    long longValue = longValue();
    if (bytesOccupied > 8 || longValue < 0) {
      return Long.MAX_VALUE;
    }
    return longValue;
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L1019-1019)
```java
    byte[] data = memoryChunk(msg.getInDataOffs().intValue(), msg.getInDataSize().intValue());
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L1042-1052)
```java
    long endowment;
    try {
      endowment = msg.getEndowment().value().longValueExact();
    } catch (ArithmeticException e) {
      if (VMConfig.allowTvmConstantinople()) {
        refundEnergy(msg.getEnergy().longValue(), "endowment out of long range");
        throw new TransferException("endowment out of long range");
      } else {
        throw e;
      }
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L1069-1069)
```java
      tokenId = String.valueOf(msg.getTokenId().longValue()).getBytes();
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L1199-1202)
```java
      int offset = msg.getOutDataOffs().intValue();
      int size = msg.getOutDataSize().intValue();

      memorySaveLimited(offset, buffer, size);
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L1697-1702)
```java
    if (!isTokenTransfer) {
      senderBalance = deposit.getBalance(senderAddress);
    } else {
      // transfer trc10 token validation
      tokenId = String.valueOf(msg.getTokenId().longValue()).getBytes();
      senderBalance = deposit.getTokenBalance(senderAddress, tokenId);
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L1768-1772)
```java
      if (VMConfig.allowTvmSelfdestructRestriction()) {
        this.memorySave(msg.getOutDataOffs().intValueSafe(), msg.getOutDataSize().intValueSafe(), out.getRight());
      } else {
        this.memorySave(msg.getOutDataOffs().intValue(), out.getRight());
      }
```
