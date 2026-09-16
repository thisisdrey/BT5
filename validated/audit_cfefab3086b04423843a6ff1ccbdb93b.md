I found a direct analog. The bug class in CVE-2023-6839 — improper error handling that leaks an internal package name in an HTTP error response — is reproduced in java-tron's own error formatting helper.

### Title
Unauthenticated HTTP API error responses leak internal Java package/class names via `Util.printErrorMsg` and `TriggerSmartContractServlet` - (File: `framework/src/main/java/org/tron/core/services/http/Util.java`)

### Summary
Several HTTP API servlets that any anonymous client can reach (`GetBurnTrxServlet`, `GetNodeInfoServlet`, `GetPendingSizeServlet`, `GetRewardServlet`, `TriggerSmartContractServlet`, and others) catch a generic `Exception` and serialize `e.getClass()` (the exception's fully-qualified class object, whose `toString()` prints `class <package>.<ClassName>`) together with `e.getMessage()` directly into the JSON `"Error"` field returned to the caller.

### Finding Description
`Util.printErrorMsg` builds the error body as: [1](#0-0) 
`jsonObject.put("Error", e.getClass() + " : " + e.getMessage())` concatenates the exception's `Class` object (rendering as `class org.tron.core.<...>.<ExceptionName>` or `class java.lang.<ExceptionName>` for JDK exceptions bubbling up from deep internals) with the raw exception message. This is invoked from multiple unauthenticated servlets on any malformed/edge-case input, e.g.: [2](#0-1) [3](#0-2) [4](#0-3) 

The same pattern independently appears in `TriggerSmartContractServlet`, which explicitly builds `e.getClass() + " : " + errString` into the protobuf `Return.message` field returned to the client: [5](#0-4) 

Because these catch blocks are generic `catch (Exception e)`, any unexpected runtime failure (e.g. a `NullPointerException`, `ClassCastException`, or an internal Tron-specific exception type like `ContractValidateException`, `ItemNotFoundException`, etc.) will have its fully-qualified internal package path echoed back to the caller — exactly the class of bug CVE-2023-6839 describes (internal package name leaked via REST error response due to improper error handling).

### Impact Explanation
This is an information-disclosure issue: it reveals internal Java package structure and exception taxonomy of the java-tron server to any anonymous HTTP API caller. It does not by itself allow unauthorized fund movement, RCE, or a crash, but it aids attacker reconnaissance (fingerprinting internal code paths/versions) and is a genuine analog to the reported WSO2 bug class. Given the strict validation criteria (only concrete account compromise/theft/freezing, crash, chain split, key disclosure, RCE, or API outage count as high-confidence impact), this finding is limited to low/medium informational disclosure and does not meet a "High/Critical, concrete asset-theft" bar — it aligns with the Medium severity of the original CVE.

### Likelihood Explanation
Trivial to trigger: any external, unauthenticated client can hit these HTTP endpoints (`/wallet/getburntrxamount`, `/wallet/getnodeinfo`, `/wallet/getpendingsize`, `/wallet/getreward`, `/wallet/triggersmartcontract`, etc.) with malformed or edge-case parameters to force an exception and observe the internal package name in the response body.

### Recommendation
Change `Util.printErrorMsg` to use a sanitized, generic error message (or `e.getClass().getSimpleName()` at most, without full package path) instead of `e.getClass()`, and apply the same fix to `TriggerSmartContractServlet`'s error-message construction. Internal package/class details should only be logged server-side (as already done via `logger.error("", e)` in some servlets), not returned in the client-facing response.

### Proof of Concept
1. Send a GET/POST request to any endpoint using `Util.printErrorMsg` (e.g. `GetRewardServlet`, `GetNodeInfoServlet`) with input that triggers an unexpected exception (e.g., malformed address causing something other than `DecoderException`/`IllegalArgumentException`, or an internal state error).
2. Observe the JSON response body contains `"Error": "class org.tron.core.exception.XyzException : <message>"`, disclosing the internal package path.
3. Alternatively, POST to `/wallet/triggersmartcontract` with a payload causing a generic exception (not `ContractValidateException`) in the `try` block; the returned `Return.message` will contain `e.getClass() + " : " + errString`, again leaking the internal package path. [1](#0-0)

### Citations

**File:** framework/src/main/java/org/tron/core/services/http/Util.java (L117-121)
```java
  public static String printErrorMsg(Exception e) {
    JSONObject jsonObject = new JSONObject();
    jsonObject.put("Error", e.getClass() + " : " + e.getMessage());
    return jsonObject.toJSONString();
  }
```

**File:** framework/src/main/java/org/tron/core/services/http/GetBurnTrxServlet.java (L26-33)
```java
    } catch (Exception e) {
      logger.error("", e);
      try {
        response.getWriter().println(Util.printErrorMsg(e));
      } catch (IOException ioe) {
        logger.debug("IOException: {}", ioe.getMessage());
      }
    }
```

**File:** framework/src/main/java/org/tron/core/services/http/GetNodeInfoServlet.java (L26-33)
```java
    } catch (Exception e) {
      logger.error("", e);
      try {
        response.getWriter().println(Util.printErrorMsg(e));
      } catch (IOException ioe) {
        logger.debug("IOException: {}", ioe.getMessage());
      }
    }
```

**File:** framework/src/main/java/org/tron/core/services/http/GetPendingSizeServlet.java (L26-33)
```java
    } catch (Exception e) {
      logger.error("", e);
      try {
        response.getWriter().println(Util.printErrorMsg(e));
      } catch (IOException ioe) {
        logger.debug("IOException: {}", ioe.getMessage());
      }
    }
```

**File:** framework/src/main/java/org/tron/core/services/http/TriggerSmartContractServlet.java (L91-98)
```java
    } catch (Exception e) {
      String errString = null;
      if (e.getMessage() != null) {
        errString = e.getMessage().replaceAll("[\"]", "\'");
      }
      retBuilder.setResult(false).setCode(response_code.OTHER_ERROR)
          .setMessage(ByteString.copyFromUtf8(e.getClass() + " : " + errString));
    }
```
