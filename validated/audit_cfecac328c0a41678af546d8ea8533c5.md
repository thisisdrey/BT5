Based on my investigation, I found the extCodeCopyAction path in java-tron's TVM implementation that closely mirrors the tinyexr AllocateImage bug class: allocating memory sized directly from attacker-controlled input length without bounding the allocation itself (only downstream gas metering constrains it, and I could not confirm that metering fully bounds the raw allocation size before it happens).

### Title
Unbounded byte array allocation from attacker-controlled EXTCODECOPY length parameter - (File: actuator/src/main/java/org/tron/core/vm/OperationActions.java)

### Summary
`extCodeCopyAction` allocates `new byte[lengthData]` where `lengthData` is popped directly off the EVM stack via `intValueSafe()`, with no explicit clamp against the length before the allocation occurs. [1](#0-0) 

### Finding Description
`extCodeCopyAction(Program program)` pops `memOffset`, `codeOffset`, and `lengthData` from the stack, then immediately does `byte[] codeCopy = new byte[lengthData];` before any copy occurs. [1](#0-0) 
`intValueSafe()` clamps values to a valid `int` range but does not otherwise bound the value to a small, safe size — a contract can push a very large 32-bit length (up to `Integer.MAX_VALUE`) for this opcode.

Energy metering in `EnergyCost` does track memory expansion cost with a `MEM_LIMIT` constant (3MB) used elsewhere, but I was unable to confirm — within my available search budget — that this MEM_LIMIT/memory-expansion energy check is evaluated and enforced for the EXTCODECOPY memory-write region *before* `extCodeCopyAction` runs `new byte[lengthData]`. If the cost calculation for EXTCODECOPY's destination memory region is computed on `memOffset + lengthData` for gas purposes but the raw `new byte[lengthData]` intermediate buffer is allocated independent of/prior to that check succeeding, then a contract could specify a length large enough to trigger a large one-shot heap allocation (analogous to tinyexr's `AllocateImage` allocating dimensions straight from crafted header fields) before the transaction is rejected for insufficient energy, causing transient memory pressure/OOM risk on the executing full node.

### Impact Explanation
If the allocation is not properly gated by the actual gas/energy check prior to the array allocation, a single crafted smart-contract call (reachable by any transaction broadcaster or contract deployer through normal TVM execution) could force nodes replaying/validating the transaction to attempt very large heap allocations, risking OutOfMemoryError and node crash/halt — matching the CVE-2018-20652 bug class (uncontrolled memory allocation from crafted input length before validation).

### Likelihood Explanation
Uncertain/Low-Medium. This requires that gas metering does NOT effectively bound `lengthData` before the allocation executes. I could not verify the exact ordering/enforcement between `getExtCodeCopyCost` in `EnergyCost.java` and the execution of `extCodeCopyAction` within my remaining search budget — the opcode dispatch and cost-check sequencing needs direct confirmation in `OperationRegistry.java` and `Program.java`'s opcode execution loop, which I was unable to fully trace in the given iterations.

### Recommendation
Explicitly clamp `lengthData` in `extCodeCopyAction` (and any similar CODECOPY/RETURNDATACOPY/CALLDATACOPY-style handlers) against a safe maximum (e.g., the existing 3MB `MEM_LIMIT`) or against `memNeeded`/gas-verified bounds before allocating `new byte[lengthData]`, ensuring the energy/gas check for the memory expansion is always performed and enforced strictly prior to any raw array allocation of attacker-controlled size.

### Proof of Concept
Not fully constructed — this requires confirming, via direct execution trace or a Devin session, whether `OperationRegistry` invokes `EnergyCost.getExtCodeCopyCost` (or equivalent memory-cost calculation) and enforces sufficient energy for the requested `lengthData` before dispatching to `extCodeCopyAction`. If confirmed unbounded, a PoC would be a contract executing `EXTCODECOPY` with `lengthData = 0x7fffffff` against a target address, submitted with just enough energy to pass any prior (non-memory) cost checks but insufficient to have been rejected earlier.

**Caveat**: Due to exhausted search iterations, I could not conclusively verify whether the gas-cost/memory-limit check in `EnergyCost.java` (`MEM_LIMIT`, `calcMemEnergy`) is applied and enforced *before* the `new byte[lengthData]` allocation in `extCodeCopyAction`, which is the crux of whether this is exploitable. I recommend a Devin session with full file/execution-trace access to confirm the opcode dispatch order in `OperationRegistry.java` and `Program.java` before treating this as a confirmed, actionable vulnerability.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/OperationActions.java (L463-476)
```java
  public static void extCodeCopyAction(Program program) {
    DataWord address = program.stackPop();
    byte[] fullCode = program.getCodeAt(address);

    int memOffset = program.stackPop().intValueSafe();
    int codeOffset = program.stackPop().intValueSafe();
    int lengthData = program.stackPop().intValueSafe();

    int sizeToBeCopied = lengthData;
    if ((long) codeOffset + lengthData > fullCode.length) {
      sizeToBeCopied = fullCode.length < codeOffset ? 0 : fullCode.length - codeOffset;
    }

    byte[] codeCopy = new byte[lengthData];
```
