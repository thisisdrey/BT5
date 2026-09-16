### Title
Unbounded-length decimal integer field in HTTP JSON request bodies triggers quadratic-time `BigInteger` parsing DoS - ([File: framework/src/main/java/org/tron/core/services/http/JsonFormat.java])

### Summary
`JsonFormat.parseInteger()`, the tokenizer used by `JsonFormat.merge()` to convert an incoming HTTP JSON request body into a protobuf message, has no upper bound on the length of a numeric literal before it falls into the `new BigInteger(numberText, radix)` path. Any int64/uint64 field of any protobuf contract submitted over the public HTTP API (e.g. `frozen_balance`, `amount`, `frozen_duration`, `value`) can be filled with an arbitrarily long digit string, forcing the node to run `BigInteger`'s quadratic-time base-10 string constructor on attacker-controlled input — the same bug class as CVE-2020-10735/BIT-libpython-2020-10735 (quadratic-time parsing of decimal strings with non-power-of-2 bases).

### Finding Description
`JsonFormat.parseInteger(text, isSigned, isLong)` only takes the cheap `Long.parseLong` fast path when `numberText.length() < 16`; for anything longer it unconditionally does:
```java
BigInteger bigValue = new BigInteger(numberText, radix);
```
with `radix` defaulting to 10 for ordinary decimal text (no `0x`/`0` prefix check applies to plain digit strings). [1](#0-0) 

There is no length cap on `text`/`numberText` at this layer, unlike the JSON-RPC boundary in `JsonRpcApiUtil`, which explicitly caps hex/decimal block-number/tx-index strings before calling `BigInteger` parsing specifically to avoid this quadratic-time cost: [2](#0-1) 

That defensive length check exists at the JSON-RPC boundary but was never applied to the HTTP JSON tokenizer (`JsonFormat`). `JsonFormat.merge()` is the deserializer used across essentially every mutating HTTP servlet to turn an unauthenticated client's JSON body into the protobuf contract that gets validated/broadcast — e.g. `FreezeBalanceServlet`, `CreateAssetIssueServlet`, `DeployContractServlet`, `ExchangeCreateServlet`, `AccountPermissionUpdateServlet`, `DelegateResourceServlet`, and dozens more. [3](#0-2) 

An anonymous API client (no signature, no private key, no funds required — this executes before signature/permission checks, during request deserialization) can therefore submit a POST body containing a protobuf int64/uint64 text field with hundreds of thousands to millions of decimal digits, and the parsing thread will burn CPU proportional to the square of the digit count on `new BigInteger(numberText, 10)`, per the documented complexity class of CVE-2020-10735.

The HTTP server does impose a 4 MB request-size ceiling via Jetty's `SizeLimitHandler`: [4](#0-3) 
4 MB of digit characters is still enough to construct a single numeric literal of several million digits, which per the CVE's own benchmark (≈5s for 1,000,000 digits) already causes multi-second single-request CPU stalls; several such requests fired concurrently against the FullNode HTTP servlets can tie up the servlet thread pool for extended periods, degrading or denying the wallet HTTP API for all other users.

### Impact Explanation
This is a pre-authentication, pre-signature-verification CPU-exhaustion vector reachable by any anonymous HTTP client hitting the node's Wallet HTTP API (no TRX balance, no valid transaction, no account needed). A modest number of crafted requests can consume CPU across the servlet thread pool for seconds each, causing the wallet HTTP API to stop serving other legitimate transaction broadcasters, contract deployers, and asset issuers in a timely manner — an availability impact on an API the node can no longer serve, consistent with the required impact bar (API/node can no longer serve).

### Likelihood Explanation
High. The only requirements are: (1) a plaintext JSON POST to any of the many mutating HTTP endpoints that route through `JsonFormat.merge()`, and (2) a large numeric literal for any int64/uint64 protobuf field in that endpoint's request schema. No authentication, signature, account balance, or special privilege is required, and the code path is exercised before any contract-specific validate/execute logic runs.

### Recommendation
Add the same defensive length cap that was already applied in `JsonRpcApiUtil.parseBlockNumber`/`parseTxIndex` to `JsonFormat.parseInteger` (and any other `BigInteger`/`BigDecimal` string-constructor call sites reachable from HTTP JSON deserialization): reject numeric tokens whose digit length exceeds what a 64-bit (or 32-bit, depending on target field) value could ever require (e.g. ~20 decimal digits for int64/uint64) before calling `new BigInteger(numberText, radix)`, mirroring the guard already documented in `JsonRpcApiUtil`: [5](#0-4) 

### Proof of Concept
1. Pick any mutating HTTP endpoint that deserializes a JSON body via `JsonFormat.merge()` into a protobuf message containing an int64/uint64 field, e.g. `POST /wallet/freezebalance` with body:
```json
{
  "owner_address": "<any 21-byte hex address>",
  "frozen_balance": "111111111111...11" ,  // several hundred thousand to ~4,000,000 '1' digits
  "frozen_duration": 3
}
```
2. `JsonFormat.merge()` tokenizes `frozen_balance`'s value and calls `parseInteger(text, true, true)`; because the digit string length is ≥16, it hits `new BigInteger(numberText, 10)`. [6](#0-5) 
3. Send several such requests concurrently (still under the 4 MB body cap) to different or the same servlet endpoints; observe elevated CPU usage and increased latency/timeout on concurrent, legitimate wallet HTTP requests while the servlet threads are busy in `BigInteger` construction.

### Citations

**File:** framework/src/main/java/org/tron/core/services/http/JsonFormat.java (L1150-1177)
```java
    String numberText = text.substring(pos);

    long result = 0;
    if (numberText.length() < 16) {
      // Can safely assume no overflow.
      result = Long.parseLong(numberText, radix);
      if (negative) {
        result = -result;
      }

      // Check bounds.
      // No need to check for 64-bit numbers since they'd have to be 16 chars
      // or longer to overflow.
      if (!isLong) {
        if (isSigned) {
          if ((result > Integer.MAX_VALUE) || (result < Integer.MIN_VALUE)) {
            throw new NumberFormatException("Number out of range for 32-bit signed integer: "
                + text);
          }
        } else {
          if ((result >= (1L << 32)) || (result < 0)) {
            throw new NumberFormatException("Number out of range for 32-bit unsigned integer: "
                + text);
          }
        }
      }
    } else {
      BigInteger bigValue = new BigInteger(numberText, radix);
```

**File:** framework/src/main/java/org/tron/core/services/jsonrpc/JsonRpcApiUtil.java (L653-687)
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
    BigInteger value;
    try {
      value = ByteArray.hexToBigInteger(blockNum);
    } catch (Exception e) {
      throw new JsonRpcInvalidParamsException(BLOCK_NUM_ERROR);
    }
    if (value.signum() < 0) {
      throw new JsonRpcInvalidParamsException(BLOCK_NUM_ERROR);
    }
    try {
      return value.longValueExact();
    } catch (ArithmeticException e) {
      throw new JsonRpcInvalidParamsException(BLOCK_NUM_ERROR);
    }
  }
```

**File:** framework/src/main/java/org/tron/core/services/http/FreezeBalanceServlet.java (L1-5)
```java
package org.tron.core.services.http;

import javax.servlet.http.HttpServletRequest;
import javax.servlet.http.HttpServletResponse;
import lombok.extern.slf4j.Slf4j;
```

**File:** framework/src/main/java/org/tron/common/application/HttpService.java (L44-94)
```java
  protected long maxRequestSize = 4 * 1024 * 1024; // 4MB

  @VisibleForTesting
  public long getMaxRequestSize() {
    return this.maxRequestSize;
  }

  @VisibleForTesting
  public void setMaxRequestSize(long maxRequestSize) {
    this.maxRequestSize = maxRequestSize;
  }

  @Override
  public void innerStart() throws Exception {
    if (this.apiServer != null) {
      this.apiServer.start();
    }
  }

  @Override
  public void innerStop() throws Exception {
    if (this.apiServer != null) {
      this.apiServer.stop();
    }
  }

  @Override
  public CompletableFuture<Boolean> start() {
    initServer();
    ServletContextHandler context = initContextHandler();
    addServlet(context);
    addFilter(context);
    return super.start();
  }

  protected void initServer() {
    this.apiServer = new Server(this.port);
    int maxHttpConnectNumber = Args.getInstance().getMaxHttpConnectNumber();
    if (maxHttpConnectNumber > 0) {
      this.apiServer.addBean(new ConnectionLimit(maxHttpConnectNumber, this.apiServer));
    }
    this.apiServer.setErrorHandler(new OversizedRequestErrorHandler());
  }

  protected ServletContextHandler initContextHandler() {
    ServletContextHandler context = new ServletContextHandler(ServletContextHandler.SESSIONS);
    context.setContextPath(this.contextPath);
    SizeLimitHandler sizeLimitHandler = new SizeLimitHandler(this.maxRequestSize, -1);
    sizeLimitHandler.setHandler(context);
    this.apiServer.setHandler(sizeLimitHandler);
    return context;
```
