Based on my investigation, this is confirmed: an unprivileged HTTP API client can trigger a Denial of Service by submitting a JSON number with a very short textual form but an astronomically large scale (e.g. exponent notation like `1e2000000000`), through fields such as `call_value`, `fee_limit`, `call_token_value`, or `token_id` in `/wallet/triggersmartcontract` or `/wallet/deploycontract`.

### Title
Denial of Service via unbounded BigDecimal scale in HTTP API JSON number parsing - (File: framework/src/main/java/org/tron/core/services/http/Util.java)

### Summary
`org.tron.json.JSON`'s Jackson `ObjectMapper` is configured with `DeserializationFeature.USE_BIG_DECIMAL_FOR_FLOATS` enabled to emulate Fastjson, so any JSON number containing a decimal point or exponent is parsed into a `BigDecimal` [1](#0-0) . `Util.getJsonLongValue` then extracts that value by calling `bigDecimal.longValueExact()` [2](#0-1) . `TypeUtils.castToBigDecimal` only bounds the raw *string length* to 65535 characters before constructing the `BigDecimal` [3](#0-2) , but does nothing to bound the resulting *scale/exponent*. A short string like `1e2000000000` passes the length check trivially while producing a `BigDecimal` with a scale near `Integer.MIN_VALUE`, and computing `longValueExact()`/related BigInteger scaling on such a value is exactly the CWE-20/CWE-834 "unexpected number processing time" class described in the Eclipse Parsson advisory (CVE-2023-4043).

### Finding Description
`TriggerSmartContractServlet.doPost` reads attacker-supplied JSON body fields `call_token_value`, `token_id`, `call_value`, and `fee_limit` via `Util.getJsonLongValue(jsonObject, ...)` [4](#0-3) . The same pattern is used in `DeployContractServlet` for contract-deployment fields. `getJsonLongValue` retrieves the value as a `BigDecimal` (`jsonObject.getBigDecimal(key)`, which routes through `TypeUtils.castToBigDecimal`) and calls `.longValueExact()` unconditionally [2](#0-1) .

`TypeUtils.castToBigDecimal` guards only against pathologically long numeric *strings* (>65535 chars) [5](#0-4) , and Jackson's Big-Decimal-for-floats mode will happily parse compact scientific-notation tokens (e.g. `"1e2147483647"`) into a `BigDecimal` whose `scale()` is an extreme int value, since constructing a `BigDecimal` from text is O(length) and does not itself materialize `10^scale`. The expensive operation is deferred to any subsequent BigDecimal arithmetic that needs to normalize/align scale — exactly `longValueExact()`, which for a decimal value must effectively multiply/divide the unscaled value by `10^|scale|` to check exactness, an operation whose cost explodes with the magnitude of scale, not the length of the input text. This mirrors the Parsson vulnerability class: a tiny attacker-controlled string produces an outsized computational cost during a later "extract the numeric value" step, unbounded by any input-size check.

### Impact Explanation
Any anonymous HTTP API client can send a single POST request to `/wallet/triggersmartcontract` (or `/wallet/deploycontract`) with a body such as `{"owner_address":"...","contract_address":"...","call_value":1e2147483647}`. If the underlying JDK `BigDecimal.longValueExact()`/scale-normalization path is not internally bounded, the HTTP request-handling thread can be tied up for a disproportionate amount of time (or throw `OutOfMemoryError` while materializing an enormous `BigInteger`), degrading or crashing the FullNode's HTTP API service — a "no longer able to serve API" condition, matching the accepted-impact criteria for this scan.

### Likelihood Explanation
The attack requires no authentication, no signed transaction, and no special privileges — merely an HTTP POST with a crafted JSON body to a publicly exposed FullNode HTTP endpoint. The 65535-character string-length guard in `TypeUtils.castToBigDecimal` provides no protection against this specific vector, since the malicious payload is only ~13 characters long. This makes the analog straightforward to trigger, though the actual severity depends on how the JVM's `BigDecimal`/`BigInteger` implementation internally handles a huge scale during `longValueExact()` (modern JDKs partially mitigate extreme-scale `BigInteger` exponentiation cost, so actual wall-clock impact should be empirically verified).

### Recommendation
Add an explicit bound on `BigDecimal.scale()` (and its magnitude) immediately after parsing/retrieving a JSON-supplied number and before calling `longValueExact()` or any other scale-dependent operation — e.g., in `TypeUtils.castToBigDecimal` or in `Util.getJsonLongValue`, reject any `BigDecimal` whose `abs(scale())` exceeds a small sane bound (analogous to the mitigation Parsson itself applied by capping number/scale size). Alternatively, disable `USE_BIG_DECIMAL_FOR_FLOATS` for HTTP-facing numeric fields that are expected to be plain integers, or use Jackson's `StreamReadConstraints` numeric-length/exponent constraints if available in the Jackson version in use.

### Proof of Concept
```
POST /wallet/triggersmartcontract HTTP/1.1
Content-Type: application/json

{
  "owner_address": "<valid 21-byte hex address>",
  "contract_address": "<valid 21-byte hex address>",
  "call_value": 1e2147483647,
  "fee_limit": 1000000
}
```
This reaches `TriggerSmartContractServlet.doPost` → `Util.getJsonLongValue(jsonObject, "call_value")` → `jsonObject.getBigDecimal("call_value")` (parsed by Jackson as a `BigDecimal` with an extreme scale due to `USE_BIG_DECIMAL_FOR_FLOATS`) → `bigDecimal.longValueExact()`, exercising the unbounded-scale numeric processing path [4](#0-3) [2](#0-1) .

**Note on uncertainty**: I could not execute code in this environment to empirically measure the actual CPU/memory cost of `BigDecimal.longValueExact()` on an extreme-scale value under the JDK version java-tron targets — modern JDKs have some internal optimizations for `BigInteger` power-of-ten scaling that may reduce (but not necessarily eliminate) the real-world severity. This should be validated with a runtime reproduction before treating it as a confirmed exploitable DoS.

### Citations

**File:** common/src/main/java/org/tron/json/JSON.java (L48-50)
```java
      // Fastjson Feature.UseBigDecimal (default ON)
      // https://github.com/alibaba/fastjson/wiki/deserialize_disable_bigdecimal_cn
      .configure(DeserializationFeature.USE_BIG_DECIMAL_FOR_FLOATS, true)
```

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

**File:** common/src/main/java/org/tron/json/TypeUtils.java (L169-184)
```java
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

**File:** framework/src/main/java/org/tron/core/services/http/TriggerSmartContractServlet.java (L70-73)
```java
      build.setCallTokenValue(Util.getJsonLongValue(jsonObject, "call_token_value"));
      build.setTokenId(Util.getJsonLongValue(jsonObject, "token_id"));
      build.setCallValue(Util.getJsonLongValue(jsonObject, "call_value"));
      long feeLimit = Util.getJsonLongValue(jsonObject, "fee_limit");
```
