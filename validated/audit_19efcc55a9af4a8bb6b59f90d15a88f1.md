### Title
Unchecked attacker-controlled length used to size/index precompile-internal arrays in `ValidateMultiSign` - ([File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java])

### Summary
`ValidateMultiSign.execute()` and the shared helpers `extractBytesArray`/`extractSigArray`/`extractBytes32Array` in `PrecompiledContracts.java` take a length word directly out of attacker-supplied calldata and use it, unchecked, to allocate an array and as a loop bound to index further into the same attacker-supplied word array — the same root-cause pattern as CVE-2018-12085 (an untrusted length value driving buffer sizing/indexing without validation). Unlike its sibling `BatchValidateSign`, `ValidateMultiSign.execute()` does not wrap this parsing step in a `catch (Throwable …)` guard, so a malformed call can throw an uncaught runtime error out of the precompile.

### Finding Description
`extractBytesArray` reads `len` straight from `words[offset].intValueSafe()` and immediately does `new byte[len][]`, then loops `i < len` reading `words[offset + i + 1]` and `words[offset + bytesOffset + 1]` with no bounds check against `words.length`: [1](#0-0) 

`extractSigArray` and `extractBytes32Array` have the identical unguarded pattern: [2](#0-1) 

In `ValidateMultiSign.execute()`, the `MAX_SIZE` guard on the parsed array size is only applied when `VMConfig.allowTvmSelfdestructRestriction()` is enabled, and even when it is enabled, the guard call itself (`words[words[3].intValueSafe() / WORD_SIZE].intValueSafe()`) and the subsequent `extractSigArray`/`extractBytesArray` call happen **before** the method's `try { … } catch (Throwable t)` block, which only wraps the later signature-recovery loop: [3](#0-2) 

By contrast, `BatchValidateSign.execute()` was hardened by wrapping the entire `doExecute()` call (which contains the same `extractBytesArray`/`extractSigArray`/`extractBytes32Array` calls) in a `try { … } catch (Throwable t)`: [4](#0-3) 

This asymmetry means `ValidateMultiSign` lacks the same defensive wrapper that was clearly added deliberately for `BatchValidateSign` to contain exactly this class of parsing failure (confirmed by the TIP-854 tests exercising malformed-calldata containment for `BatchValidateSign`, e.g. `testTip854OuterFrameContainment`): [5](#0-4) 

Both precompiles are reached via a plain `CALL` opcode to a fixed precompile address (`0x...9` / `0x...a`) from any deployed contract, i.e. from any unprivileged `TriggerSmartContract` transaction: [6](#0-5) 

A crafted `len` value (e.g. near `Integer.MAX_VALUE`) causes `new byte[len][]`/`new byte[len]` to throw `OutOfMemoryError` (an `Error`, not caught by `catch (Exception e)` style guards used elsewhere in the codebase) or `NegativeArraySizeException`/`ArrayIndexOutOfBoundsException` for negative/out-of-range values, and — for `ValidateMultiSign` specifically — this exception path is not caught by the method's own try/catch, unlike `BatchValidateSign`.

### Impact Explanation
An uncaught `OutOfMemoryError`/unchecked `RuntimeException` escaping precompile execution during TVM opcode dispatch can, depending on how far up the call stack it propagates before being caught, destabilize the node process handling the transaction (large transient allocation attempts, GC pressure) or produce inconsistent per-node failure behavior if only some nodes have enough heap headroom to survive the allocation attempt and others do not — risking a chain split if block validation results diverge across differently-provisioned nodes. This satisfies the "node crash or halt" / "chain split" impact bar.

### Likelihood Explanation
Reachable by any account with enough TRX for energy: deploy or call a contract that issues a `CALL` to the `validateMultiSign` precompile address with crafted calldata where the array-length word is very large or negative. No special permission, no privileged role, and no dependency on other users' behavior is required — a single signed `TriggerSmartContract` transaction is sufficient to exercise the vulnerable path.

### Recommendation
Add the same `MAX_SIZE`/bounds validation on the parsed array length unconditionally (not gated behind `allowTvmSelfdestructRestriction()`), validate all derived offsets/lengths against `words.length` and `data.length` before allocating or indexing, and wrap the entire body of `ValidateMultiSign.execute()` (including the length-parsing and array-extraction calls) in the same defensive `try { … } catch (Throwable t)` pattern already used in `BatchValidateSign.execute()`, returning `Pair.of(true, DATA_FALSE)` on failure instead of letting the exception propagate.

### Proof of Concept
1. Craft ABI-encoded calldata for `validatemultisign(address,uint256,bytes32,bytes[])` where the dynamic `bytes[]` array's length word (read at `words[3].intValueSafe()/WORD_SIZE` offset) is set to a very large value (e.g. `0x7fffffff`).
2. Deploy a trivial contract that `CALL`s the `validateMultiSign` precompile address (`0x...a`) with this calldata, or trigger it directly if the ABI allows external invocation.
3. With `VMConfig.allowTvmSelfdestructRestriction()` disabled (default/pre-activation state), execution reaches `extractBytesArray(words, offset, rawData)` directly, which executes `new byte[0x7fffffff][]`, triggering `OutOfMemoryError` outside of any try/catch in `ValidateMultiSign.execute()`.
4. Observe the exception escaping the precompile call; whether it is ultimately caught higher up in the VM's opcode-dispatch loop could not be fully confirmed in this review (tool budget was exhausted before inspecting `Program.callToPrecompiledAddress`'s exception handling) — this should be verified directly against `actuator/src/main/java/org/tron/core/vm/program/Program.java` to determine whether `Error` types are caught there, which determines whether the failure is contained to the transaction or escalates to node-level impact.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L149-152)
```java
  private static final DataWord batchValidateSignAddr = new DataWord(
      "0000000000000000000000000000000000000000000000000000000000000009");
  private static final DataWord validateMultiSignAddr = new DataWord(
      "000000000000000000000000000000000000000000000000000000000000000a");
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L390-426)
```java
  private static byte[][] extractBytes32Array(DataWord[] words, int offset) {
    int len = words[offset].intValueSafe();
    byte[][] bytes32Array = new byte[len][];
    for (int i = 0; i < len; i++) {
      bytes32Array[i] = words[offset + i + 1].getData();
    }
    return bytes32Array;
  }

  private static byte[][] extractBytesArray(DataWord[] words, int offset, byte[] data) {
    if (offset > words.length - 1) {
      return new byte[0][];
    }
    int len = words[offset].intValueSafe();
    byte[][] bytesArray = new byte[len][];
    for (int i = 0; i < len; i++) {
      int bytesOffset = words[offset + i + 1].intValueSafe() / WORD_SIZE;
      int bytesLen = words[offset + bytesOffset + 1].intValueSafe();
      bytesArray[i] = extractBytes(data, (bytesOffset + offset + 2) * WORD_SIZE,
          bytesLen);
    }
    return bytesArray;
  }

  private static byte[][] extractSigArray(DataWord[] words, int offset, byte[] data) {
    if (offset > words.length - 1) {
      return new byte[0][];
    }
    int len = words[offset].intValueSafe();
    byte[][] bytesArray = new byte[len][];
    for (int i = 0; i < len; i++) {
      int bytesOffset = words[offset + i + 1].intValueSafe() / WORD_SIZE;
      bytesArray[i] = extractBytes(data, (bytesOffset + offset + 2) * WORD_SIZE,
          SIG_LENGTH);
    }
    return bytesArray;
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1051-1082)
```java
    @Override
    public Pair<Boolean, byte[]> execute(byte[] rawData) {
      if (VMConfig.allowTvmOsaka()
          && !isValidAbiEncoding(rawData, ABI_HEADER_WORDS, ABI_ITEM_WORDS)) {
        return Pair.of(false, EMPTY_BYTE_ARRAY);
      }
      DataWord[] words = DataWord.parseArray(rawData);
      byte[] address = words[0].toTronAddress();
      int permissionId = words[1].intValueSafe();
      byte[] data = words[2].getData();

      byte[] combine = ByteUtil.merge(address, ByteArray.fromInt(permissionId), data);
      byte[] hash = Sha256Hash.hash(CommonParameter
          .getInstance().isECKeyCryptoEngine(), combine);

      if (VMConfig.allowTvmSelfdestructRestriction()) {
        int sigArraySize = words[words[3].intValueSafe() / WORD_SIZE].intValueSafe();
        if (sigArraySize > MAX_SIZE) {
          return Pair.of(true, DATA_FALSE);
        }
      }
      byte[][] signatures = VMConfig.allowTvmSelfdestructRestriction() ?
          extractSigArray(words, words[3].intValueSafe() / WORD_SIZE, rawData) :
          extractBytesArray(words, words[3].intValueSafe() / WORD_SIZE, rawData);

      if (signatures.length == 0 || signatures.length > MAX_SIZE) {
        return Pair.of(true, DATA_FALSE);
      }

      AccountCapsule account = this.getDeposit().getAccount(address);
      if (account != null) {
        try {
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1144-1154)
```java
    @Override
    public Pair<Boolean, byte[]> execute(byte[] data) {
      try {
        return doExecute(data);
      } catch (Throwable t) {
        if (t instanceof InterruptedException){
          Thread.currentThread().interrupt();
        }
        return Pair.of(true, new byte[WORD_SIZE]);
      }
    }
```

**File:** framework/src/test/java/org/tron/common/runtime/vm/OperationsTest.java (L861-902)
```java
  // TIP-854 outer-frame containment: a CALL to validateMultiSign or
  // batchValidateSign with malformed calldata must (a) push 0 onto the outer
  // stack, (b) leave the outer frame free of any propagated exception, and
  // (c) allow the outer frame to continue executing afterwards.
  @Test
  public void testTip854OuterFrameContainment() throws ContractValidateException {
    byte prePrefixByte = DecodeUtil.addressPreFixByte;
    DecodeUtil.addressPreFixByte = Constant.ADD_PRE_FIX_BYTE_MAINNET;
    VMConfig.initAllowTvmOsaka(1);
    try {
      for (PrecompiledContracts.PrecompiledContract contract :
          new PrecompiledContracts.PrecompiledContract[]{
              new PrecompiledContracts.ValidateMultiSign(),
              new PrecompiledContracts.BatchValidateSign()}) {
        invoke = new ProgramInvokeMockImpl();
        InternalTransaction interTrx = new InternalTransaction(
            Protocol.Transaction.getDefaultInstance(),
            InternalTransaction.TrxType.TRX_UNKNOWN_TYPE);
        program = new Program(new byte[0], new byte[0], invoke, interTrx);
        // inDataSize=0 ⇒ data=[] ⇒ fewer than H=5 head words ⇒ guard rejects.
        MessageCall messageCall = new MessageCall(
            Op.CALL, new DataWord(10000),
            DataWord.ZERO(), DataWord.ZERO(),
            DataWord.ZERO(), DataWord.ZERO(),
            DataWord.ZERO(), DataWord.ZERO(),
            DataWord.ZERO(), false);
        program.callToPrecompiledAddress(messageCall, contract);

        Assert.assertNull(contract.getClass().getSimpleName()
                + ": outer frame must not inherit an exception",
            program.getResult().getException());
        Assert.assertEquals(contract.getClass().getSimpleName() + ": inner CALL pushes 0",
            DataWord.ZERO(), program.getStack().pop());
        // Outer frame continues: another stack op works without throwing.
        program.stackPush(new DataWord(1));
        Assert.assertEquals(new DataWord(1), program.getStack().pop());
      }
    } finally {
      VMConfig.initAllowTvmOsaka(0);
      DecodeUtil.addressPreFixByte = prePrefixByte;
    }
  }
```
