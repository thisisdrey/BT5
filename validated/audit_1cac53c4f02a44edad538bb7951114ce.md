## Analog Found

### Title
Unbounded numeric string passed to `new BigInteger(..., 10)` in shielded TRC-20 amount parsing allows CPU denial of service - (File: `framework/src/main/java/org/tron/core/Wallet.java`)

### Summary
The Jackson CVE class is: an attacker-controlled string is handed directly to a quadratic-cost numeric constructor (`BigInteger(String)`/`BigDecimal(String)`) with no length pre-check, allowing a small request to trigger large CPU cost. `Wallet.getBigIntegerFromString` reproduces exactly this pattern: it calls `new BigInteger(trimmedIn, 10)` on a raw client-supplied string with no length bound before parsing.

### Finding Description
`Wallet.getBigIntegerFromString` is defined as: [1](#0-0) 
```
private BigInteger getBigIntegerFromString(String in) {
    String trimmedIn = in.trim();
    if (trimmedIn.length() == 0) {
      return BigInteger.ZERO;
    }
    return new BigInteger(trimmedIn, 10);
}
```
No maximum-length guard is applied before parsing. It is called with client-controlled `String` fields taken straight from protobuf request messages:
- `createShieldedContractParametersWithoutAsk` → `getBigIntegerFromString(request.getFromAmount())` and `getBigIntegerFromString(request.getToAmount())` [2](#0-1) 
- `getTriggerInputForShieldedTRC20Contract` → `getBigIntegerFromString(request.getAmount())` [3](#0-2) 

Both are directly reachable, unauthenticated HTTP POST endpoints:
- `CreateShieldedContractParametersWithoutAskServlet.doPost` builds `PrivateShieldedTRC20ParametersWithoutAsk` from the raw JSON body via `JsonFormat.merge` and calls `wallet.createShieldedContractParametersWithoutAsk(...)`. [4](#0-3) 
- `GetTriggerInputForShieldedTRC20ContractServlet.doPost` does the same for `ShieldedTRC20TriggerContractParameters` and calls `wallet.getTriggerInputForShieldedTRC20Contract(...)`. [5](#0-4) 

`fromAmount`/`toAmount`/`amount` are `string` typed protobuf fields (arbitrary length, bounded only by the general HTTP body size limit, not by any numeric-token length constraint like jackson-core's `StreamReadConstraints.maxNumberLength`). `new BigInteger(text, 10)` internally performs repeated multiply-by-radix accumulation, which is quadratic in digit count for very long decimal strings — the same class of cost blow-up as the JDK's `BigInteger(String)` constructor referenced in the CVE. The length/range validation (`checkBigIntegerRange`, bit-length ≤ 256) only happens **after** the expensive parse has already completed, so it provides no protection against the CPU cost of parsing.

Both call sites are gated by `checkAllowShieldedTransactionApi()`, which throws unless the node operator has enabled the shielded transaction API (a config flag, `vm.allowShieldedTransactionApi`). This is a configuration-gate rather than an authentication/permission check — any anonymous client can hit the endpoint once the feature is enabled, which is common on nodes that support shielded/private transfers (a standard, documented API feature, not a special or privileged mode).

### Impact Explanation
An anonymous HTTP client can submit a POST body containing a `fromAmount`/`toAmount`/`amount` string consisting of millions of decimal digits. Parsing that string with `new BigInteger(text, 10)` forces the single request-handling thread to perform CPU work that grows quadratically with the digit count, similarly to the jackson-databind CVE. A handful of concurrent requests of a few megabytes each can consume all available worker threads / CPU, degrading or halting the node's ability to serve other API requests — a legitimate, in-scope "API the node can no longer serve" denial-of-service impact.

### Likelihood Explanation
Likelihood is bounded by the fact that the shielded TRC-20 API must be enabled (`allowShieldedTransactionApi`), which is not guaranteed to be on by default on every node, so this is not universally reachable. Where it is enabled (a supported and documented feature for privacy-transfer support), the exploit requires no authentication, no special permissions, and no crafted proofs — just an oversized numeric string in a JSON field, making it trivial to trigger once the feature is on.

### Recommendation
Add a maximum input-length check (e.g., a small bound like 32–80 characters, enough for any legitimate 256-bit amount) in `getBigIntegerFromString` before calling `new BigInteger(trimmedIn, 10)`, rejecting oversized strings with a `ContractValidateException`/`ZksnarkException` immediately — mirroring the length-guard pattern already used elsewhere in the codebase, e.g. `JsonRpcApiUtil.parseBlockNumber`'s `MAX_BLOCK_NUM_HEX_LEN` check that explicitly documents "API-level DoS guard: rejects pathological inputs before BigInteger parsing, whose cost grows quadratically with length." [6](#0-5) 

### Proof of Concept
1. Enable the shielded TRC-20 API (`vm.allowShieldedTransactionApi=true`) on a target full node (a supported feature for privacy transfers).
2. Send an HTTP POST to `/wallet/createshieldedcontractparameterswithoutask` (or `/wallet/gettriggerinputforshieldedtrc20contract`) with a JSON body where `from_amount` (or `amount`) is a string of several million `'9'` characters, e.g.:
```json
{"shielded_TRC20_contract_address": "<21-byte address hex>", "from_amount": "9999...9 (several million digits)", "to_amount": "0", ...}
```
3. `JsonFormat.merge` parses the JSON body into the request message (the `string` field itself is not size-restricted by numeric-token limits), then `Wallet.getBigIntegerFromString` calls `new BigInteger(trimmedIn, 10)` on the multi-million-digit string, consuming large single-threaded CPU time before the subsequent `checkBigIntegerRange` bit-length check ever runs. Repeating the request concurrently a handful of times exhausts server worker threads/CPU.

### Citations

**File:** framework/src/main/java/org/tron/core/Wallet.java (L3762-3771)
```java
    BigInteger fromAmount;
    BigInteger toAmount;
    try {
      fromAmount = getBigIntegerFromString(request.getFromAmount());
      toAmount = getBigIntegerFromString(request.getToAmount());
    } catch (Exception e) {
      throw new ContractValidateException("invalid_from amount or to_amount");
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

**File:** framework/src/main/java/org/tron/core/Wallet.java (L4321-4329)
```java
  public BytesMessage getTriggerInputForShieldedTRC20Contract(
      ShieldedTRC20TriggerContractParameters request)
      throws ZksnarkException, ContractValidateException {
    checkAllowShieldedTransactionApi();

    ShieldedTRC20Parameters shieldedTRC20Parameters = request.getShieldedTRC20Parameters();
    List<BytesMessage> spendAuthoritySignature = request.getSpendAuthoritySignatureList();
    BigInteger value = getBigIntegerFromString(request.getAmount());
    checkBigIntegerRange(value);
```

**File:** framework/src/main/java/org/tron/core/services/http/CreateShieldedContractParametersWithoutAskServlet.java (L23-36)
```java
  protected void doPost(HttpServletRequest request, HttpServletResponse response) {
    try {
      PostParams params = PostParams.getPostParams(request);
      PrivateShieldedTRC20ParametersWithoutAsk.Builder build =
          PrivateShieldedTRC20ParametersWithoutAsk.newBuilder();
      JsonFormat.merge(params.getParams(), build, params.isVisible());
      ShieldedTRC20Parameters shieldedTRC20Parameters = wallet
          .createShieldedContractParametersWithoutAsk(build.build());
      response.getWriter().println(JsonFormat
              .printToString(shieldedTRC20Parameters, params.isVisible()));
    } catch (Exception e) {
      Util.processError(e, response);
    }
  }
```

**File:** framework/src/main/java/org/tron/core/services/http/GetTriggerInputForShieldedTRC20ContractServlet.java (L23-35)
```java
  protected void doPost(HttpServletRequest request, HttpServletResponse response) {
    try {
      PostParams params = PostParams.getPostParams(request);
      ShieldedTRC20TriggerContractParameters.Builder builder =
          ShieldedTRC20TriggerContractParameters
              .newBuilder();
      JsonFormat.merge(params.getParams(), builder, params.isVisible());
      BytesMessage result = wallet.getTriggerInputForShieldedTRC20Contract(builder.build());
      response.getWriter().println(JsonFormat.printToString(result, params.isVisible()));
    } catch (Exception e) {
      Util.processError(e, response);
    }
  }
```

**File:** framework/src/main/java/org/tron/core/services/jsonrpc/JsonRpcApiUtil.java (L653-672)
```java
  /**
   * Max allowed length for a JSON-RPC block number hex/decimal input.
   * API-level DoS guard: rejects pathological inputs before BigInteger parsing,
   * whose cost grows quadratically with length. A block number fits a signed long,
   * so the longest valid input is 19 chars (decimal Long.MAX_VALUE) or 18 (0x + 16
   * hex); 20 leaves a small margin.
   */
  private static final int MAX_BLOCK_NUM_HEX_LEN = 20;

  /**
   * Parse a JSON-RPC block number (hex "0x..." or decimal) into a long,
   * enforcing the {@link #MAX_BLOCK_NUM_HEX_LEN} length limit, rejecting
   * negative values, and rejecting values that overflow a signed 64-bit
   * block number.
   */
  public static long parseBlockNumber(String blockNum)
      throws JsonRpcInvalidParamsException {
    if (blockNum == null || blockNum.length() > MAX_BLOCK_NUM_HEX_LEN) {
      throw new JsonRpcInvalidParamsException(BLOCK_NUM_ERROR);
    }
```
