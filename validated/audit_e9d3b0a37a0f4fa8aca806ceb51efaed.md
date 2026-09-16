### Title
Unbounded BigDecimal exponent materialization in HTTP JSON numeric parsing causes DoS - ([File: framework/src/main/java/org/tron/core/services/http/Util.java])

### Summary
`Util.getJsonLongValue()`, used by multiple unauthenticated HTTP API servlets (`DeployContractServlet`, `TriggerSmartContractServlet`, `GetExchangeByIdServlet`, `GetProposalByIdServlet`) to parse attacker-controlled numeric JSON fields such as `fee_limit`, `call_value`, `call_token_value`, `token_id`, converts the field to a `BigDecimal` and calls `longValueExact()` on it. This is the same bug class as CVE-2020-15225 (django-filter `NumberFilter`): a small, cheaply-constructed numeric literal using scientific/exponential notation can force an expensive integer materialization step, consuming disproportionate CPU/memory for a single request.

### Finding Description
`Util.getJsonLongValue()` reads the raw JSON value and converts it to `long`: [1](#0-0) 

`jsonObject.getBigDecimal(key)` delegates to `TypeUtils.castToBigDecimal()`: [2](#0-1) 

When the JSON body contains the value as a native JSON number (not a quoted string), `JSONObject.convertNode()` returns the already-parsed Jackson `decimalValue()`, and `castToBigDecimal()` returns it directly via the `value instanceof BigDecimal` branch — bypassing the 65535-character string-length guard that would otherwise apply to string-typed input: [3](#0-2) 

`BigDecimal` construction from a textual exponential form (e.g. `1e999999999`) is cheap — it just stores an unscaled value and an `int` scale, without expanding digits. The blow-up happens later, when `longValueExact()` is called: internally this invokes `toBigIntegerExact()`, which for a negative scale (i.e., a positive exponent) must materialize `unscaledValue * 10^(-scale)` as a full `BigInteger` in order to check exactness. For an exponent in the hundreds of millions, this single multiplication allocates and computes a number with hundreds of millions of decimal digits, consuming large amounts of heap and CPU from a payload only a few bytes long.

The Jackson `ObjectMapper` used for HTTP JSON parsing (`org.tron.json.JSON`) only overrides `maxNestingDepth` and `maxTokenCount` in its `StreamReadConstraints`, leaving the default `maxNumberLength` (1000 characters) in effect: [4](#0-3) 

A 1000-character cap bounds the textual length of the number token, but does **not** bound the *magnitude* it can represent via exponential notation — a token like `"1e900000000"` is only 12 characters yet encodes an exponent near 9×10^8, which is large enough to exhaust memory/CPU when materialized.

Reachable, unauthenticated call sites include:
- `TriggerSmartContractServlet.doPost` — `fee_limit`, `call_value`, `call_token_value`, `token_id`: [5](#0-4) 
- `DeployContractServlet.doPost` — `call_token_value`, `token_id`, `call_value`, `consume_user_resource_percent`, `origin_energy_limit`, `fee_limit`: [6](#0-5) 
- `GetExchangeByIdServlet.doPost` / `GetProposalByIdServlet.doPost` — `id`: [7](#0-6) 

### Impact Explanation
An anonymous HTTP API client can submit a tiny JSON body (well under any size limit) containing an exponential-notation number for any of these fields. The resulting `longValueExact()` call triggers `BigInteger` exponentiation proportional to the encoded exponent, which can consume gigabytes of heap and substantial CPU on the request-handling thread. Repeated or concurrent requests can exhaust JVM heap (leading to `OutOfMemoryError`) or pin CPU, degrading or crashing the node's HTTP API — a resource-exhaustion Denial-of-Service against a full node's externally reachable service, consistent in class with the referenced CVE-2020-15225.

### Likelihood Explanation
High: no authentication or special privileges are required, the payload is trivial to craft (a short JSON string), and the vulnerable code path (`Util.getJsonLongValue` → `TypeUtils.castToBigDecimal` → `BigDecimal.longValueExact()`) is exercised by several commonly used, publicly documented HTTP endpoints (deploy contract, trigger smart contract, query exchange/proposal by id).

### Recommendation
- In `TypeUtils.castToBigDecimal()` / `Util.getJsonLongValue()`, reject `BigDecimal` values whose `precision()`/unscaled digit count or absolute `scale()` exceeds a small sane bound (e.g., the number of digits needed to represent `Long.MAX_VALUE`) before calling `longValueExact()`.
- Alternatively, avoid `longValueExact()`/`toBigIntegerExact()` entirely for this conversion and instead validate the scale/precision cheaply first (e.g., `bigDecimal.precision() - bigDecimal.scale() > 19` or similar) and throw a fast, safe error rather than letting `BigDecimal` perform the exponent materialization.
- Configure a stricter Jackson `StreamReadConstraints` (or add manual validation) that limits not just the number token length but also detects tokens whose value, if materialized, would exceed a small digit-count ceiling.

### Proof of Concept
Send, to a node's HTTP API port, a POST to `/wallet/triggersmartcontract` (or `/wallet/deploycontract`) with a body such as:

```json
{
  "owner_address": "4100000000000000000000000000000000000000",
  "contract_address": "4100000000000000000000000000000000000000",
  "fee_limit": 1e900000000
}
```

`Util.getJsonLongValue(jsonObject, "fee_limit")` will parse `1e900000000` into a `BigDecimal` (cheap) and then call `longValueExact()`, which internally computes `10^900000000` as a `BigInteger` — allocating/consuming memory and CPU disproportionate to the tiny request size, degrading or crashing the servlet thread / JVM heap.

*Note: I was not able to execute this against a live node to empirically measure resource consumption; the analysis is based on the documented behavior of `java.math.BigDecimal.longValueExact()`/`toBigIntegerExact()` and the code paths cited above.*

### Citations

**File:** framework/src/main/java/org/tron/core/services/http/Util.java (L507-513)
```java
  public static long getJsonLongValue(JSONObject jsonObject, String key, boolean required) {
    BigDecimal bigDecimal = jsonObject.getBigDecimal(key);
    if (required && bigDecimal == null) {
      throw new InvalidParameterException("key [" + key + "] does not exist");
    }
    return (bigDecimal == null) ? 0L : bigDecimal.longValueExact();
  }
```

**File:** common/src/main/java/org/tron/json/TypeUtils.java (L150-184)
```java
  static BigDecimal castToBigDecimal(Object value) {
    if (value == null) {
      return null;
    }

    if (value instanceof Float) {
      if (Float.isNaN((Float) value) || Float.isInfinite((Float) value)) {
        return null;
      }
    } else if (value instanceof Double) {
      if (Double.isNaN((Double) value) || Double.isInfinite((Double) value)) {
        return null;
      }
    } else if (value instanceof BigDecimal) {
      return (BigDecimal) value;
    } else if (value instanceof BigInteger) {
      return new BigDecimal((BigInteger) value);
    }

    String strVal = value.toString();

    if (strVal.isEmpty() || "null".equalsIgnoreCase(strVal)) {
      return null;
    }

    if (strVal.length() > 65535) {
      throw new JSONException("decimal overflow");
    }

    if (strVal.indexOf(',') != -1) {
      strVal = strVal.replaceAll(",", "");
    }

    return new BigDecimal(strVal);
  }
```

**File:** common/src/main/java/org/tron/json/JSONObject.java (L135-140)
```java
    if (child.isBigInteger()) {
      return child.bigIntegerValue();
    }
    if (child.isBigDecimal()) {
      return child.decimalValue();
    }
```

**File:** common/src/main/java/org/tron/json/JSON.java (L62-67)
```java
  private static JsonFactory buildFactory() {
    return JsonFactory.builder().streamReadConstraints(StreamReadConstraints.builder()
            .maxNestingDepth(Constant.MAX_NESTING_DEPTH)
            .maxTokenCount(Constant.MAX_TOKEN_COUNT)
            .build()).build();
  }
```

**File:** framework/src/main/java/org/tron/core/services/http/TriggerSmartContractServlet.java (L70-73)
```java
      build.setCallTokenValue(Util.getJsonLongValue(jsonObject, "call_token_value"));
      build.setTokenId(Util.getJsonLongValue(jsonObject, "token_id"));
      build.setCallValue(Util.getJsonLongValue(jsonObject, "call_value"));
      long feeLimit = Util.getJsonLongValue(jsonObject, "fee_limit");
```

**File:** framework/src/main/java/org/tron/core/services/http/DeployContractServlet.java (L45-80)
```java
      build.setCallTokenValue(Util.getJsonLongValue(jsonObject, "call_token_value"))
          .setTokenId(Util.getJsonLongValue(jsonObject, "token_id"));
      ABI.Builder abiBuilder = ABI.newBuilder();
      if (jsonObject.containsKey("abi")) {
        String abi = jsonObject.getString("abi");
        StringBuffer abiSB = new StringBuffer("{");
        abiSB.append("\"entrys\":");
        abiSB.append(abi);
        abiSB.append("}");
        JsonFormat.merge(abiSB.toString(), abiBuilder, params.isVisible());
      }
      SmartContract.Builder smartBuilder = SmartContract.newBuilder();
      smartBuilder
          .setAbi(abiBuilder)
          .setCallValue(Util.getJsonLongValue(jsonObject, "call_value"))
          .setConsumeUserResourcePercent(Util.getJsonLongValue(jsonObject,
              "consume_user_resource_percent"))
          .setOriginEnergyLimit(Util.getJsonLongValue(jsonObject, "origin_energy_limit"));
      if (!ArrayUtils.isEmpty(ownerAddress)) {
        smartBuilder.setOriginAddress(ByteString.copyFrom(ownerAddress));
      }

      String jsonByteCode = jsonObject.getString("bytecode");
      if (jsonObject.containsKey("parameter")) {
        jsonByteCode += jsonObject.getString("parameter");
      }
      byte[] byteCode = ByteArray.fromHexString(jsonByteCode);
      if (!ArrayUtils.isEmpty(byteCode)) {
        smartBuilder.setBytecode(ByteString.copyFrom(byteCode));
      }
      String name = jsonObject.getString("name");
      if (!Strings.isNullOrEmpty(name)) {
        smartBuilder.setName(name);
      }

      long feeLimit = Util.getJsonLongValue(jsonObject, "fee_limit");
```

**File:** framework/src/main/java/org/tron/core/services/http/GetExchangeByIdServlet.java (L22-31)
```java
  protected void doPost(HttpServletRequest request, HttpServletResponse response) {
    try {
      PostParams params = PostParams.getPostParams(request);
      JSONObject jsonObject = JSONObject.parseObject(params.getParams());
      long id = Util.getJsonLongValue(jsonObject, "id", true);
      fillResponse(params.isVisible(), id, response);
    } catch (Exception e) {
      Util.processError(e, response);
    }
  }
```
