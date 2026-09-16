### Title
Unbounded decimal-string parsing in shielded TRC-20 amount fields enables verifier-side/API-level CPU-exhaustion DoS - (File: `framework/src/main/java/org/tron/core/Wallet.java`)

### Summary
`Wallet.getBigIntegerFromString` parses the `fromAmount`, `toAmount`, and `amount` string fields of shielded TRC-20 requests with `new BigInteger(trimmedIn, 10)` and no length bound, before any range check is applied. These fields are reachable from unauthenticated HTTP/gRPC endpoints, letting a remote caller submit an arbitrarily long decimal string (megabytes of digits) and force the node to perform expensive big-integer parsing (quadratic-time), analogous to the reported gnark `frontend.NewWitness` DoS.

### Finding Description
`getBigIntegerFromString` trims the input and calls `new BigInteger(trimmedIn, 10)` directly with no maximum length check: [1](#0-0) 

This helper is invoked with caller-supplied strings before any bit-length validation:
- `createShieldedContractParametersWithoutAsk` parses `request.getFromAmount()` / `request.getToAmount()` and only calls `checkBigIntegerRange` (which just checks non-negativity and bit length) *after* the expensive `new BigInteger(..., 10)` parse has already completed: [2](#0-1) 
- `getTriggerInputForShieldedTRC20Contract` similarly parses `request.getAmount()` via the same unbounded helper before checking its range: [3](#0-2) 

Both entry points are reachable via the JSON HTTP API without authentication:
- `CreateShieldedContractParametersWithoutAskServlet.doPost` merges arbitrary client-supplied JSON directly into the `PrivateShieldedTRC20ParametersWithoutAsk` protobuf (which has an unbounded `string` field for the amount) and forwards it to `wallet.createShieldedContractParametersWithoutAsk`: [4](#0-3) 
- `GetTriggerInputForShieldedTRC20ContractServlet` and the corresponding gRPC service in `RpcApiService` expose the same call chain to remote clients.

Unlike `JsonFormat.parseInteger` (used elsewhere for 32/64-bit values), which explicitly documents and enforces bounded-length parsing before falling back to `BigInteger`, and unlike the new `JsonRpcApiUtil.parseBlockNumber` guard that caps input length before `BigInteger` construction — see the deliberate DoS mitigation comment there — `getBigIntegerFromString` has no equivalent guard: [5](#0-4) 

### Impact Explanation
An anonymous HTTP/gRPC client can submit a `fromAmount`/`toAmount`/`amount` field consisting of a very long decimal digit string (bounded only by the max request/gRPC message size, potentially megabytes). `BigInteger(String, int)` construction cost is superlinear in the number of digits, so this can consume significant CPU per request on a node's API-serving thread, degrading availability of the node's JSON/gRPC endpoints for other clients — a concrete "API the node can no longer serve" condition, matching the accepted impact categories.

### Likelihood Explanation
The shielded TRC-20 API endpoints are gated only by `checkAllowShieldedTransactionApi()` (a network parameter that must be enabled) — no per-caller authentication is otherwise required, and the endpoints are designed to be called by any wallet client. If shielded TRC-20 support is enabled on the network (a runtime chain parameter), any anonymous API caller can trigger this with a single crafted POST request; no special privileges are needed. Likelihood is Medium, contingent on the shielded-transaction feature flag being enabled, which is the same contingency the underlying feature already assumes.

### Recommendation
In `getBigIntegerFromString`, reject inputs whose length exceeds the maximum plausible digit count for a 256-bit (or otherwise bounded) amount (e.g., ~78 decimal digits) before calling `new BigInteger`, mirroring the approach already used in `JsonRpcApiUtil.parseBlockNumber`. Perform this length check prior to constructing the `BigInteger`, not just the subsequent `checkBigIntegerRange` bit-length check, so that the expensive parse is never attempted on oversized input.

### Proof of Concept
1. Enable/assume shielded TRC-20 transactions are allowed on the target node.
2. Send an HTTP POST to `/wallet/createshieldedcontractparameterswithoutask` (or the gRPC equivalent) with `from_amount` (or `to_amount`) set to a decimal string containing several million digits (e.g., `"1" + "0"*5_000_000`).
3. Observe `Wallet.getBigIntegerFromString` invoking `new BigInteger(trimmedIn, 10)` on the oversized string before any length/range validation occurs, consuming disproportionate CPU/memory on the API-serving thread.
4. Repeat concurrently to degrade the node's ability to serve other HTTP/gRPC API requests.

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

**File:** framework/src/main/java/org/tron/core/Wallet.java (L4326-4329)
```java
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
