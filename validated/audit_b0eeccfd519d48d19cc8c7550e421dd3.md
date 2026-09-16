### Title
Unescaped JSON construction in `JsonFormat.printErrorMsg` allows JSON injection / response corruption via attacker-controlled exception messages - (File: framework/src/main/java/org/tron/core/services/http/JsonFormat.java)

### Summary
`JsonFormat.printErrorMsg`, used by `Util.processError` and directly by numerous HTTP API servlets, builds an error JSON response by string-concatenating the raw exception message into a hand-built JSON string without any escaping of quotes, backslashes, or control characters. This mirrors the Tomcat `JsonErrorReportValve` bug class (CWE-116/CWE-74): untrusted data that reaches an error field is not properly encoded for the JSON output context.

### Finding Description
`JsonFormat.printErrorMsg` constructs the response manually: [1](#0-0) 
It appends `ex.getMessage()` directly between quotes with no call to any JSON-escaping routine (no handling of `"`, `\`, or control characters such as `\r`/`\n`).

This method is the single choke point used by `Util.processError`, which is called from a large number of unprivileged, transaction/contract-facing HTTP endpoints on exception paths: [2](#0-1) 

Examples of reachable callers include `BroadcastHexServlet.doPost` [3](#0-2) , `CreateCommonTransactionServlet.doPost` [4](#0-3) , `FreezeBalanceServlet.doPost` [5](#0-4) , `UnFreezeBalanceV2Servlet.doPost` [6](#0-5) , `GetContractServlet` [7](#0-6) , `GetContractInfoServlet` [8](#0-7) , and `GetBlockServlet` [9](#0-8) . `GetRewardServlet` and `GetTransactionInfoByBlockNumServlet` call `Util.printErrorMsg` directly on their exception paths as well [10](#0-9) [11](#0-10) . Other servlets (`GetRewardServlet`, `GetBrokerageServlet`) build similarly unescaped ad-hoc error JSON inline with the same flaw: [12](#0-11) 

Many exception paths reachable by any unauthenticated caller embed attacker-supplied text into `e.getMessage()`. In particular, `JsonFormat.merge`'s tokenizer throws `ParseException`s that quote a portion of the raw malformed JSON input back in the message (e.g. `"Check the input for a valid JSON format."` context built from tokenizer state) [13](#0-12) , and other library exceptions (address decoders, hex/base64 decoders, JSON parsers) frequently include a snippet of the caller-supplied string. When any of that string contains a double quote, backslash, or newline, the resulting `{"Error":"<msg>"}` document is no longer valid/predictable JSON: additional key/value pairs can be injected, the document can be terminated early, or CRLF-style content can be smuggled into the body that downstream tooling naively splits/parses as JSON lines.

Note: `TriggerSmartContractServlet` / `TriggerConstantContractServlet` / `EstimateEnergyServlet` already defensively call `e.getMessage().replaceAll("[\"]", "'")` before embedding the message [14](#0-13) , which shows the project is aware quote-escaping is required here — but this sanitization is not applied consistently, and `JsonFormat.printErrorMsg`/`Util.processError`, the most widely-shared error-formatting utility, has no such protection.

### Impact Explanation
Any unauthenticated HTTP API caller (transaction broadcaster, contract deployer, query client) can trigger an exception whose message is reflected verbatim, unescaped, into a JSON HTTP response. This can corrupt the JSON structure for any client/integration parsing these responses (wallets, exchanges, monitoring tooling), potentially causing them to misinterpret fields (e.g., injecting a fabricated `"result": true` or additional keys next to the intended error), or causing parser failures/DoS in strict downstream JSON consumers. This matches CWE-116/CWE-74 (improper output encoding leading to injection into the JSON output context), consistent with the referenced Tomcat advisory's bug class. It does not by itself grant remote code execution or direct fund theft; the impact is scoped to response-integrity/JSON-injection against clients of the node's HTTP API.

### Likelihood Explanation
High reachability: the vulnerable formatting path (`Util.processError` → `JsonFormat.printErrorMsg`) is the default error handler for a very large fraction of the `/wallet` HTTP servlets, all reachable pre-auth over the network. Triggering an exception with attacker-influenced text in the message (e.g., malformed hex/address/JSON payloads) is trivial and requires no special privileges.

### Recommendation
Escape all string values embedded in hand-built JSON (`JsonFormat.printErrorMsg` and the inline `"{...}"` constructions in `GetRewardServlet`/`GetBrokerageServlet`) using a proper JSON string escaper (e.g., serialize via `org.tron.json.JSONObject`/Jackson `ObjectMapper` instead of raw `StringBuilder` concatenation) so that quotes, backslashes, and control characters are correctly encoded, matching the fix pattern already used for the `type`/`message` fields in the referenced Tomcat commits.

### Proof of Concept
Send a request to any endpoint that funnels exceptions through `Util.processError`/`JsonFormat.printErrorMsg` with input designed to produce an exception message containing a double quote, e.g., POST to `/wallet/getcontract` with a malformed `value` causing a decoding exception whose message reflects the raw (attacker supplied) string containing `"`,`\n`, or `\`. The resulting HTTP body `{"Error":"<injected-content>"}` will contain broken/injected JSON structure instead of a single escaped error string, observable by capturing the raw HTTP response bytes.

### Citations

**File:** framework/src/main/java/org/tron/core/services/http/JsonFormat.java (L296-301)
```java
    // Test to make sure the tokenizer has reached the end of the stream.
    if (!tokenizer.atEnd()) {
      throw tokenizer.parseException(
          "Expecting the end of the stream, but there seems to be more data!  "
              + "Check the input for a valid JSON format.");
    }
```

**File:** framework/src/main/java/org/tron/core/services/http/JsonFormat.java (L304-313)
```java
  public static String printErrorMsg(Exception ex) {
    StringBuilder text = new StringBuilder();
    text.append("{");
    text.append("\"Error\":");
    text.append("\"");
    text.append(ex.getMessage());
    text.append("\"");
    text.append("}");
    return text.toString();
  }
```

**File:** framework/src/main/java/org/tron/core/services/http/Util.java (L528-535)
```java
  public static void processError(Exception e, HttpServletResponse response) {
    logger.debug(e.getMessage(), e);
    try {
      response.getWriter().println(Util.printErrorMsg(e));
    } catch (IOException ioe) {
      logger.debug("IOException: {}", ioe.getMessage());
    }
  }
```

**File:** framework/src/main/java/org/tron/core/services/http/BroadcastHexServlet.java (L41-43)
```java
    } catch (Exception e) {
      Util.processError(e, response);
    }
```

**File:** framework/src/main/java/org/tron/core/services/http/CreateCommonTransactionServlet.java (L39-41)
```java
    } catch (Exception e) {
      Util.processError(e, response);
    }
```

**File:** framework/src/main/java/org/tron/core/services/http/FreezeBalanceServlet.java (L33-35)
```java
    } catch (Exception e) {
      Util.processError(e, response);
    }
```

**File:** framework/src/main/java/org/tron/core/services/http/UnFreezeBalanceV2Servlet.java (L34-36)
```java
    } catch (Exception e) {
      Util.processError(e, response);
    }
```

**File:** framework/src/main/java/org/tron/core/services/http/GetContractServlet.java (L44-46)
```java
    } catch (Exception e) {
      Util.processError(e, response);
    }
```

**File:** framework/src/main/java/org/tron/core/services/http/GetContractInfoServlet.java (L44-46)
```java
    } catch (Exception e) {
      Util.processError(e, response);
    }
```

**File:** framework/src/main/java/org/tron/core/services/http/GetBlockServlet.java (L38-40)
```java
    } catch (Exception e) {
      Util.processError(e, response);
    }
```

**File:** framework/src/main/java/org/tron/core/services/http/GetRewardServlet.java (L38-45)
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

**File:** framework/src/main/java/org/tron/core/services/http/GetTransactionInfoByBlockNumServlet.java (L54-61)
```java
    } catch (Exception e) {
      logger.debug("Exception: {}", e.getMessage());
      try {
        response.getWriter().println(Util.printErrorMsg(e));
      } catch (IOException ioe) {
        logger.debug("IOException: {}", ioe.getMessage());
      }
    }
```

**File:** framework/src/main/java/org/tron/core/services/http/GetBrokerageServlet.java (L29-35)
```java
    } catch (DecoderException | IllegalArgumentException e) {
      try {
        response.getWriter()
            .println("{\"Error\": " + "\"INVALID address, " + e.getMessage() + "\"}");
      } catch (IOException ioe) {
        logger.debug("IOException: {}", ioe.getMessage());
      }
```

**File:** framework/src/main/java/org/tron/core/services/http/TriggerSmartContractServlet.java (L91-97)
```java
    } catch (Exception e) {
      String errString = null;
      if (e.getMessage() != null) {
        errString = e.getMessage().replaceAll("[\"]", "\'");
      }
      retBuilder.setResult(false).setCode(response_code.OTHER_ERROR)
          .setMessage(ByteString.copyFromUtf8(e.getClass() + " : " + errString));
```
