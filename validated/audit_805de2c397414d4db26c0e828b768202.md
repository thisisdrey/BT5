Based on my research, I found a valid analog. The vulnerability class from CVE-2016-2073 — a parser reading a length/index field directly from untrusted input without bounds validation, causing an out-of-bounds read/crash — maps to `PrecompiledContracts.extractSigArray`/`extractBytesArray` in java-tron's TVM precompile layer, reachable by any account that sends a transaction or makes a call that triggers a smart contract invoking the `ValidateMultiSign` precompile (address `0x...a`).

### Title
Unvalidated length field in `ValidateMultiSign` precompile calldata causes uncaught `NegativeArraySizeException`/`ArrayIndexOutOfBoundsException` during TVM execution - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
`PrecompiledContracts.extractSigArray` and `extractBytesArray` derive an array length directly from attacker-controlled calldata (`words[offset].intValueSafe()`) and then allocate/iterate using that value with no upper- or lower-bound validation on the raw value itself, only a single sanity check that can be defeated by a negative length. [1](#0-0) 

### Finding Description
In `ValidateMultiSign.execute()`, when `VMConfig.allowTvmSelfdestructRestriction()` is enabled, the code computes `sigArraySize` and rejects it only if `sigArraySize > MAX_SIZE`: [2](#0-1) 

Because `intValueSafe()` can return a negative `int` for a crafted 32-byte word, an attacker can make `sigArraySize` negative, which passes the `> MAX_SIZE` check (a negative number is never greater than 5). Execution then falls into `extractSigArray`, where `len` is reused as a negative value and `new byte[len][]` throws `NegativeArraySizeException`: [3](#0-2) 

When `allowTvmSelfdestructRestriction()` is disabled (the legacy/default code path), there is no length check at all before calling `extractBytesArray`/`extractSigArray`, so an attacker can supply an arbitrarily large positive `len`, causing the loop to read `words[offset + i + 1]` far past the actual `words` array bounds, throwing `ArrayIndexOutOfBoundsException`, or attempting to allocate an oversized array (`OutOfMemoryError`).

Critically, unlike its sibling precompile `BatchValidateSign`, whose `execute()` explicitly wraps `doExecute()` in a `try { ... } catch (Throwable t) { return Pair.of(true, new byte[WORD_SIZE]); }` to contain exactly this class of failure: [4](#0-3) 

`ValidateMultiSign.execute()` has **no such enclosing try/catch** around the `extractSigArray`/`extractBytesArray` call — the only `try/catch` in that method wraps the later account/permission verification logic, not the array-extraction step: [5](#0-4) 

This mirrors the libxml2 bug class: a length/offset value taken directly from untrusted input drives array indexing/allocation with insufficient bounds checking before the "real" parsing/validation logic runs.

### Impact Explanation
An unprivileged party can trigger this by deploying or calling any smart contract that performs a `CALL`/`STATICCALL` to the `ValidateMultiSign` precompiled contract address with crafted calldata (a length word that decodes to a negative or huge `int`). The resulting uncaught `RuntimeException`/`Error` escapes `PrecompiledContracts.ValidateMultiSign.execute()` uncontained. I was not able to fully trace, within the available iterations, whether the TVM's generic opcode dispatcher (`OperationActions`/`Program`) has an outer catch-all that safely converts this into a normal VM revert for every code path (constant call vs. full block-application call), so I cannot conclusively confirm whether the worst-case outcome is limited to a failed/reverted transaction (Low impact) or propagates further during block application in `Manager`, which — if uncontained there — would cause identical deterministic exceptions on every full node validating the same block (a synchronized chain halt/crash). Given the explicit defensive pattern already applied to the twin precompile `BatchValidateSign` (documented in code and tests, e.g. `BatchValidateSignContractTest.testTip854RejectsMalformedCalldata`) but conspicuously absent from `ValidateMultiSign`, this asymmetry itself indicates the maintainers consider uncaught exceptions from these array-extraction helpers a real, previously-addressed risk class in one precompile but not the other. [6](#0-5) 

### Likelihood Explanation
High likelihood of triggering the exception: any account can deploy a trivial contract and send one transaction calling `ValidateMultiSign` at address `0x...a` with hand-crafted calldata; no special permissions, stake, or SR/witness status are required. What remains uncertain (and needs verification with full access to `Program.java`/`OperationActions.java`) is exactly how far the resulting exception propagates and whether it is safely caught before reaching block-application logic in `Manager`.

### Recommendation
Add the same defensive containment already used in `BatchValidateSign` to `ValidateMultiSign.execute()`: wrap the calldata parsing/extraction (including `extractSigArray`/`extractBytesArray` and the `sigArraySize` computation) in a try/catch that returns `Pair.of(true/false, DATA_FALSE/EMPTY_BYTE_ARRAY)` on any `Throwable`. Additionally, harden `extractSigArray`/`extractBytesArray`/`extractBytes32Array` themselves to reject negative or out-of-range `len`/`offset` values before allocating or indexing, rather than relying solely on call-site checks that can be bypassed by negative-length attacks.

### Proof of Concept
1. Craft ABI-encoded calldata for `validatemultisign(address,uint256,bytes32,bytes[])` where the dynamic-array length word for the `bytes[]` parameter is set to `0xFFFFFFFF` (decodes to `-1` via `intValueSafe()`).
2. Deploy a contract that forwards this calldata via a low-level `call` to precompile address `0x000000000000000000000000000000000000000a`.
3. Broadcast a `TriggerSmartContract` transaction (or constant call) invoking that function.
4. With `allowTvmSelfdestructRestriction` enabled, `sigArraySize` evaluates to `-1`, bypasses the `> MAX_SIZE` guard, and `extractSigArray` executes `new byte[-1][]`, throwing `NegativeArraySizeException` uncaught by `ValidateMultiSign.execute()` (contrast with `BatchValidateSignContractTest.testTip854RejectsMalformedCalldata`, which shows the sibling precompile has been hardened against structurally similar malformed-length calldata while `ValidateMultiSign` has not). [7](#0-6)

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L414-426)
```java
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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1051-1080)
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

**File:** framework/src/test/java/org/tron/common/runtime/vm/BatchValidateSignContractTest.java (L134-172)
```java
  // TIP-854: after activation, batchValidateSign (H=5, I=6) must reject calldata
  // whose byte length is incompatible with the (words - 5) / 6 shape the per-call
  // energy formula already assumes, returning (false, empty). The guard lives in
  // doExecute(); the outer try/catch does not mask it because the guard does not
  // throw (pure arithmetic + a static getter).
  @Test
  public void testTip854RejectsMalformedCalldata() {
    contract.setVmShouldEndInUs(System.nanoTime() / 1000 + 2_000_000);
    VMConfig.initAllowTvmOsaka(1);
    try {
      // Bucket 1: 32-aligned head + sub-word trailing bytes (r=1, r=31).
      for (int r : new int[]{1, 31}) {
        byte[] data = new byte[(5 + 6) * 32 + r];
        Pair<Boolean, byte[]> ret = contract.execute(data);
        Assert.assertFalse("non-32-aligned len=" + data.length, ret.getLeft());
        Assert.assertSame(ByteUtil.EMPTY_BYTE_ARRAY, ret.getRight());
      }
      // Bucket 2: fewer than the static head's 5 words.
      for (int bytes : new int[]{0, 32, 64, 96, 128}) {
        Pair<Boolean, byte[]> ret = contract.execute(new byte[bytes]);
        Assert.assertFalse("len=" + bytes + " < 5 words", ret.getLeft());
        Assert.assertSame(ByteUtil.EMPTY_BYTE_ARRAY, ret.getRight());
      }
      // Bucket 3: 32-aligned but tail not a multiple of I=6 words (k = 1..5).
      for (int k = 1; k <= 5; k++) {
        byte[] data = new byte[(5 + k) * 32];
        Pair<Boolean, byte[]> ret = contract.execute(data);
        Assert.assertFalse("aligned bad-tail k=" + k, ret.getLeft());
        Assert.assertSame(ByteUtil.EMPTY_BYTE_ARRAY, ret.getRight());
      }
      // Null calldata: explicit spec clause.
      Pair<Boolean, byte[]> ret = contract.execute(null);
      Assert.assertFalse("null calldata", ret.getLeft());
      Assert.assertSame(ByteUtil.EMPTY_BYTE_ARRAY, ret.getRight());
    } finally {
      VMConfig.initAllowTvmOsaka(0);
    }
    System.gc();
  }
```
