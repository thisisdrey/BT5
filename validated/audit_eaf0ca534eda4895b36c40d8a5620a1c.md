### Title
Unbounded numeric-string field in JSON request body triggers quadratic `BigInteger` parsing in `JsonFormat.parseInteger`, enabling CPU-exhaustion DoS on the HTTP API - (File: framework/src/main/java/org/tron/core/services/http/JsonFormat.java)

### Summary
`JsonFormat.parseInteger()`, used by `JsonFormat.merge()` to deserialize JSON request bodies into protobuf messages for essentially every HTTP API servlet (transaction creation, queries, etc.), falls back to `new BigInteger(numberText, radix)` whenever a numeric token is 16 or more characters long, with no upper bound on the length of `numberText`. `BigInteger(String, radix)` construction has quadratic (O(n²)) time complexity in the number of digits, which is exactly the bug class described in the OpenSSL `OBJ_obj2txt()` report (unbounded numeric sub-identifier text conversion causing quadratic slowdown). An unauthenticated HTTP client can submit a JSON body containing an `int64`/`uint64`/`int32`/`uint32` field whose value is a string of millions of digits, forcing the node to spend large amounts of CPU time on a single request thread.

### Finding Description
`parseInteger` in `JsonFormat.java` is the numeric primitive parser invoked from `Tokenizer.consumeInt32/consumeUInt32/consumeInt64/consumeUInt64` (`framework/src/main/java/org/tron/core/services/http/JsonFormat.java:1523-1568`), which are in turn called from `handlePrimitive()` (`JsonFormat.java:705-794`) while merging arbitrary JSON text into a protobuf `Message.Builder` via `JsonFormat.merge(...)` — the standard entry point used by essentially all HTTP servlets, e.g. `CreateCommonTransactionServlet.doPost` (`framework/src/main/java/org/tron/core/services/http/CreateCommonTransactionServlet.java:33-34`), `GetBlockServlet.buildRequest` (`framework/src/main/java/org/tron/core/services/http/GetBlockServlet.java:61-67`), `GetCanDelegatedMaxSizeServlet.doPost`, `GetContractInfoServlet.doPost`, and dozens of others.

Inside `parseInteger` (`JsonFormat.java:1129-1213`):
```
String numberText = text.substring(pos);
...
} else {
  BigInteger bigValue = new BigInteger(numberText, radix);
  ...
}
```
there is no length check on `numberText` before constructing the `BigInteger`. Any protobuf field typed `int32/uint32/int64/uint64` reachable through `JsonFormat.merge` accepts an attacker-supplied numeric string of arbitrary length taken straight from the tokenizer, with the only gating being the general JSON tokenizer, which does not impose a numeric-token length limit (`JsonFormat.java` Tokenizer section, confirmed no `MAX_*_LEN` or token-length guard exists for numeric tokens).

This mirrors CVE-2023-2650 precisely: an attacker-controlled variable-length numeric representation (OpenSSL's OID sub-identifier / here a JSON integer literal) is converted via an O(n²) algorithm (`OBJ_obj2txt()` / `BigInteger(String, radix)`), and the calling code path (any protobuf message field parse via HTTP JSON) has no size cap analogous to OpenSSL's missing OID-length limit.

Notably, the codebase already recognizes and mitigates this exact bug class elsewhere: `JsonRpcApiUtil.parseBlockNumber` explicitly caps input length before calling `ByteArray.hexToBigInteger`, with a comment stating "API-level DoS guard: rejects pathological inputs before BigInteger parsing, whose cost grows quadratically with length" (`framework/src/main/java/org/tron/core/services/jsonrpc/JsonRpcApiUtil.java:653-660`). No equivalent guard exists in `JsonFormat.parseInteger`, which is a much more broadly-reachable code path (used by virtually all HTTP-API `doPost` servlets that call `JsonFormat.merge`).

### Impact Explanation
An unauthenticated remote client can send a single crafted HTTP POST to any endpoint that parses request JSON via `JsonFormat.merge` (e.g. `/wallet/createtransaction`, `/wallet/getblock`, `/wallet/getcontractinfo`, etc.) with a numeric field value containing a very large digit string. Each such request forces expensive `BigInteger` construction, consuming disproportionate CPU on the servlet request thread. Because the servlet thread pool is shared, a small number of such requests can degrade or exhaust node responsiveness, resulting in a Denial of Service against the HTTP query/broadcast API — consistent with the "API the node can no longer serve" impact criterion.

### Likelihood Explanation
High reachability: no authentication, no special privileges, and no fee cost are required — any anonymous HTTP client can send such a request. The vulnerable code path is exercised on essentially every JSON-based HTTP API request that includes an int32/int64/uint32/uint64 field, making it trivially reachable and repeatable.

### Recommendation
Add a maximum length check on `numberText` in `JsonFormat.parseInteger` before invoking `new BigInteger(numberText, radix)` (analogous to the `MAX_BLOCK_NUM_HEX_LEN` guard already present in `JsonRpcApiUtil.parseBlockNumber`), rejecting numeric tokens that exceed the maximum possible digit count for a 64-bit value (e.g., ~20-25 characters depending on radix) with a `NumberFormatException`/`ParseException` before attempting the `BigInteger` conversion.

### Proof of Concept
1. Send an HTTP POST to any endpoint using `JsonFormat.merge`, e.g. `/wallet/getcandelegatedmaxsize`, with body:
```
{"owner_address":"41...", "type": 111111111111111111111111111111...(millions of '1' digits)...1}
```
2. `JsonFormat.merge` tokenizes `type` as an integer field, invokes `Tokenizer.consumeUInt32/Int32`, which calls `parseInteger`, which (since the token length is ≥16) executes `new BigInteger(numberText, 10)` on the multi-million-digit string.
3. Because `BigInteger` string parsing is O(n²), request CPU time grows quadratically with the digit count, causing significant thread-time consumption per request and enabling low-cost, repeatable resource-exhaustion DoS against the HTTP API. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** framework/src/main/java/org/tron/core/services/http/JsonFormat.java (L705-754)
```java
  private static Object handlePrimitive(Tokenizer tokenizer, FieldDescriptor field,
      boolean selfType)
      throws ParseException {
    Object value = null;
    if ("null".equals(tokenizer.currentToken())) {
      tokenizer.consume("null");
      return value;
    }
    switch (field.getType()) {
      case INT32:
      case SINT32:
      case SFIXED32:
        value = tokenizer.consumeInt32();
        break;

      case INT64:
      case SINT64:
      case SFIXED64:
        value = tokenizer.consumeInt64();
        break;

      case UINT32:
      case FIXED32:
        value = tokenizer.consumeUInt32();
        break;

      case UINT64:
      case FIXED64:
        value = tokenizer.consumeUInt64();
        break;

      case FLOAT:
        value = tokenizer.consumeFloat();
        break;

      case DOUBLE:
        value = tokenizer.consumeDouble();
        break;

      case BOOL:
        value = tokenizer.consumeBoolean();
        break;

      case STRING:
        value = tokenizer.consumeString();
        break;

      case BYTES:
        value = tokenizer.consumeByteString(field.getFullName(), selfType);
        break;
```

**File:** framework/src/main/java/org/tron/core/services/http/JsonFormat.java (L1129-1213)
```java
  private static long parseInteger(String text, boolean isSigned, boolean isLong)
      throws NumberFormatException {
    int pos = 0;

    boolean negative = false;
    if (text.startsWith("-", pos)) {
      if (!isSigned) {
        throw new NumberFormatException("Number must be positive: " + text);
      }
      ++pos;
      negative = true;
    }

    int radix = 10;
    if (text.startsWith("0x", pos)) {
      pos += 2;
      radix = 16;
    } else if (text.startsWith("0", pos)) {
      radix = 8;
    }

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
      if (negative) {
        bigValue = bigValue.negate();
      }

      // Check bounds.
      if (!isLong) {
        if (isSigned) {
          if (bigValue.bitLength() > 31) {
            throw new NumberFormatException("Number out of range for 32-bit signed integer: "
                + text);
          }
        } else {
          if (bigValue.bitLength() > 32) {
            throw new NumberFormatException("Number out of range for 32-bit unsigned integer: "
                + text);
          }
        }
      } else {
        if (isSigned) {
          if (bigValue.bitLength() > 63) {
            throw new NumberFormatException("Number out of range for 64-bit signed integer: "
                + text);
          }
        } else {
          if (bigValue.bitLength() > 64) {
            throw new NumberFormatException("Number out of range for 64-bit unsigned integer: "
                + text);
          }
        }
      }

      result = bigValue.longValueExact();
    }

    return result;
  }
```

**File:** framework/src/main/java/org/tron/core/services/http/JsonFormat.java (L1519-1568)
```java
    /**
     * If the next token is a 32-bit signed integer, consume it and return its value. Otherwise,
     * throw a {@link ParseException}.
     */
    public int consumeInt32() throws ParseException {
      try {
        int result = parseInt32(currentToken);
        nextToken();
        return result;
      } catch (NumberFormatException e) {
        throw integerParseException(e);
      }
    }

    /**
     * If the next token is a 32-bit unsigned integer, consume it and return its value. Otherwise,
     * throw a {@link ParseException}.
     */
    public int consumeUInt32() throws ParseException {
      try {
        int result = parseUInt32(currentToken);
        nextToken();
        return result;
      } catch (NumberFormatException e) {
        throw integerParseException(e);
      }
    }

    /**
     * If the next token is a 64-bit signed integer, consume it and return its value. Otherwise,
     * throw a {@link ParseException}.
     */
    public long consumeInt64() throws ParseException {
      try {
        long result = parseInt64(currentToken);
        nextToken();
        return result;
      } catch (NumberFormatException e) {
        throw integerParseException(e);
      }
    }

    /**
     * If the next token is a 64-bit unsigned integer, consume it and return its value. Otherwise,
     * throw a {@link ParseException}.
     */
    public long consumeUInt64() throws ParseException {
      try {
        long result = parseUInt64(currentToken);
        nextToken();
```

**File:** framework/src/main/java/org/tron/core/services/http/CreateCommonTransactionServlet.java (L27-42)
```java
  protected void doPost(HttpServletRequest request, HttpServletResponse response) {
    try {
      PostParams params = PostParams.getPostParams(request);
      String contract = params.getParams();
      boolean visible = params.isVisible();
      ContractType type = ContractType.valueOf(Util.getContractType(contract));
      Message.Builder build = getBuilder(type);
      JsonFormat.merge(contract, build, visible);
      Transaction tx = wallet.createTransactionCapsule(build.build(), type).getInstance();
      JSONObject jsonObject = JSONObject.parseObject(contract);
      tx = Util.setTransactionPermissionId(jsonObject, tx);
      response.getWriter().println(Util.printCreateTransaction(tx, visible));
    } catch (Exception e) {
      Util.processError(e, response);
    }
  }
```

**File:** framework/src/main/java/org/tron/core/services/http/GetBlockServlet.java (L61-67)
```java
  private BlockReq buildRequest(String params, boolean visible)
      throws JsonFormat.ParseException {
    BlockReq.Builder build = BlockReq.newBuilder();
    if (!Strings.isNullOrEmpty(params)) {
      JsonFormat.merge(params, build, visible);
    }
    return build.build();
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
