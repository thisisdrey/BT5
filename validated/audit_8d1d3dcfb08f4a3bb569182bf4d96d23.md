### Title
Unbounded-length numeric string parsed into `BigInteger` before validation in shielded TRC-20 API - ([File: framework/src/main/java/org/tron/core/Wallet.java])

### Summary
The Go `math/big.Rat.SetString`/`UnmarshalText` bug class is a panic/resource-exhaustion vulnerability caused by parsing an attacker-controlled numeric string with no size bound before the parser does expensive work. The same root-cause pattern exists in java-tron's `Wallet.getBigIntegerFromString`, which calls `new BigInteger(trimmedIn, 10)` on a raw client-supplied string with no length check performed first.

### Finding Description
`Wallet.getBigIntegerFromString(String in)` trims the input and immediately constructs a `BigInteger` from it with no upper bound on string length: [1](#0-0) 

This method is invoked directly on client-supplied strings (`request.getFromAmount()`, `request.getToAmount()`, `request.getAmount()`) in three externally reachable Wallet API entry points used to build shielded TRC-20 transaction parameters and trigger inputs: [2](#0-1) [3](#0-2) 

Range/validity checking (`checkBigIntegerRange`) is only performed *after* the `BigInteger` object has already been constructed from the raw string, so it cannot bound the cost of the parse itself: [4](#0-3) 

By contrast, the JSON-RPC block-number parser in this codebase already recognizes and defends against exactly this bug class — it explicitly caps input length *before* calling `BigInteger` parsing, with a comment calling out that "BigInteger parsing... cost grows quadratically with length": [5](#0-4) 

`getBigIntegerFromString` has no equivalent guard, making it the weakest analog of the reported bug class in the reachable API surface.

### Impact Explanation
An anonymous or unprivileged API client that can reach `createShieldedContractParameters`, `createShieldedContractParametersWithoutAsk`, or `getTriggerInputForShieldedTRC20Contract` (gated only by the `allowShieldedTransactionApi` feature flag, not by authentication or authorization) can submit an extremely long decimal digit string as `fromAmount`/`toAmount`/`amount`. Constructing a `BigInteger` from a very large digit string is CPU- and memory-intensive; on servicing threads this can cause excessive CPU consumption, large temporary allocations, or (depending on JVM/version and input size) an `OutOfMemoryError`, degrading or crashing the node process that serves this API — a node-crash/API-denial impact in the same class as the reported Go panic.

### Likelihood Explanation
Likelihood depends on whether the shielded transaction API is enabled on a given deployment (it is a fully-implemented, first-class Wallet API method rather than a debug/test-only path) and on whether upstream gRPC/HTTP framing imposes a message-size cap that would limit how large the string can be. No such length limit exists at the `getBigIntegerFromString` call site itself, unlike the hardened `parseBlockNumber` path in the same codebase, indicating this specific call site was not hardened against the class of bug described in the report.

### Recommendation
Add an explicit maximum-length check on the trimmed input string in `getBigIntegerFromString` before calling `new BigInteger(...)`, analogous to the `MAX_BLOCK_NUM_HEX_LEN` guard already used in `JsonRpcApiUtil.parseBlockNumber`, and reject/City with a validation error for oversized inputs prior to any parsing work.

### Proof of Concept
1. Enable the shielded transaction API (`allowShieldedTransactionApi`).
2. Send a `PrivateShieldedTRC20Parameters` (or `ShieldedTRC20TriggerContractParameters`) request via the Wallet gRPC/HTTP API with `fromAmount` (or `amount`) set to a string of many millions of decimal digits (bounded only by the transport's max message size).
3. Observe elevated CPU/memory consumption during `getBigIntegerFromString` → `new BigInteger(trimmedIn, 10)`, before any range validation occurs, potentially causing service degradation or `OutOfMemoryError` on repeated/concurrent requests.

### Citations

**File:** framework/src/main/java/org/tron/core/Wallet.java (L3626-3636)
```java
    BigInteger fromAmount;
    BigInteger toAmount;
    try {
      fromAmount = getBigIntegerFromString(request.getFromAmount());
      toAmount = getBigIntegerFromString(request.getToAmount());
    } catch (Exception e) {
      throw new ContractValidateException("invalid from_amount or to_amount");
    }

    long[] scaledPublicAmount = checkPublicAmount(shieldedTRC20ContractAddress,
        fromAmount, toAmount);
```

**File:** framework/src/main/java/org/tron/core/Wallet.java (L4212-4218)
```java
  private BigInteger getBigIntegerFromString(String in) {
    String trimmedIn = in.trim();
    if (trimmedIn.length() == 0) {
      return BigInteger.ZERO;
    }
    return new BigInteger(trimmedIn, 10);
  }
```

**File:** framework/src/main/java/org/tron/core/Wallet.java (L4223-4226)
```java
  private long[] checkPublicAmount(byte[] address, BigInteger fromAmount, BigInteger toAmount)
      throws ContractExeException, ContractValidateException {
    checkBigIntegerRange(fromAmount);
    checkBigIntegerRange(toAmount);
```

**File:** framework/src/main/java/org/tron/core/Wallet.java (L4328-4329)
```java
    BigInteger value = getBigIntegerFromString(request.getAmount());
    checkBigIntegerRange(value);
```

**File:** framework/src/main/java/org/tron/core/services/jsonrpc/JsonRpcApiUtil.java (L653-660)
```java
  /**
   * Max allowed length for a JSON-RPC block number hex/decimal input.
   * API-level DoS guard: rejects pathological inputs before BigInteger parsing,
   * whose cost grows quadratically with length. A block number fits a signed long,
   * so the longest valid input is 19 chars (decimal Long.MAX_VALUE) or 18 (0x + 16
   * hex); 20 leaves a small margin.
   */
  private static final int MAX_BLOCK_NUM_HEX_LEN = 20;
```
