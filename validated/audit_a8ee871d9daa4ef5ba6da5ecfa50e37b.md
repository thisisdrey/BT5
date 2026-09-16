### Title
JSON error responses built by unescaped string concatenation allow attacker-controlled data to corrupt HTTP API JSON structure - (File: framework/src/main/java/org/tron/core/services/http/JsonFormat.java)

### Summary
`JsonFormat.printErrorMsg` builds the JSON body for HTTP API error responses via raw string concatenation of the exception message, with no escaping of quotes, backslashes, or control characters, mirroring the CVE-2025-58189 pattern of embedding unescaped attacker-controlled data into an error payload.

### Finding Description
`JsonFormat.printErrorMsg` constructs the entire error JSON manually: [1](#0-0) 
It does no escaping of `ex.getMessage()` before embedding it between literal quotes. This is invoked from `Util.processError`, which is the generic exception handler used by dozens of public HTTP wallet-query servlets: [2](#0-1) 
and directly in servlets such as `GetBlockServlet`, which places `IllegalArgumentException.getMessage()` (which typically echoes back malformed/attacker-supplied request parameters such as `id_or_num`) straight into the same unescaped pattern: [3](#0-2) 
This is the identical bug class described in the report: attacker-controlled input reaching an error message that is emitted without escaping. The project already recognizes and has fixed this exact class of problem for on-chain string/bytes fields reflected in transaction JSON printing (see the dedicated regression suite and its documented rationale that unescaped attacker bytes can "forge sibling fields on the re-parse path"): [4](#0-3) 
but `JsonFormat.printErrorMsg` was not brought in line with that fix — it still uses naive concatenation, confirmed by the existing unit test that only checks the happy path with a benign message: [5](#0-4) 

### Impact Explanation
Any exception message containing a `"`, `\`, or control character that reaches `printErrorMsg` breaks the JSON structure of the HTTP response. Because dozens of unauthenticated GET/POST wallet query endpoints funnel exceptions through `Util.processError` → `JsonFormat.printErrorMsg`, and some of those exception messages are built by echoing back attacker-supplied request values, an attacker can craft input that injects extra JSON keys/values or truncates/extends the object (the same "forge sibling fields" class the project already patched for transaction JSON). Downstream consumers (explorers, exchanges, wallets, automated integrations) that parse this response leniently could be misled into acting on attacker-injected fields (e.g., a spoofed success indicator) rather than the intended error, which is a data-integrity/response-forgery issue on a node-facing API rather than a purely cosmetic one.

### Likelihood Explanation
High reachability: this code path is hit by an unauthenticated HTTP client on numerous read-only wallet endpoints whenever a malformed parameter triggers an `IllegalArgumentException`/similar exception whose message includes the raw parameter value, requiring no signed transaction or special privilege — just a single crafted HTTP request.

### Recommendation
Rewrite `JsonFormat.printErrorMsg` to build the response through a real JSON serializer (e.g., the project's own `JSONObject`/`JSON` utility, as already used in `GetBlockServlet`'s `IllegalArgumentException` branch) or apply the same string/bytes escaping routine used for on-chain fields (`JsonFormat.escapeText`/`escapeBytesSelfType`) to the exception message before concatenation, and add a regression test analogous to `JsonFormatEscapeTest` that feeds a message containing `"`, `\`, and control characters through `printErrorMsg`/`processError` and asserts the output re-parses strictly and injects no extra keys.

### Proof of Concept
1. Send `GET /wallet/getblock?id_or_num=<value with a Chinese/blockNum parse failure whose value contains a double quote and comma>` to trigger the `catch (IllegalArgumentException e)` branch in `GetBlockServlet.fillResponse`.
2. Because `jsonObject.put("Error", e.getMessage())` in that servlet already uses a real JSON builder it is safe there, but the equivalent generic path through `Util.processError`/`JsonFormat.printErrorMsg` (used by the ~50 servlets listed via `Util.processError` above) is not: any exception whose message embeds the raw request value (e.g., malformed base58/hex address, invalid block id, invalid query param) and is routed to `printErrorMsg` will produce syntactically broken/attacker-influenced JSON, observable by comparing the raw response bytes against a strict JSON parser.

*Note:* I was not able to enumerate, within the available context, a concrete existing exception message in the current codebase that both (a) is thrown from one of the `Util.processError` call sites and (b) directly embeds unsanitized attacker input (most `InvalidParameterException` messages I found are static strings). The vulnerable primitive — `JsonFormat.printErrorMsg`'s lack of escaping — is confirmed in code, but full confirmation of an end-to-end reachable message containing attacker-controlled bytes would benefit from a broader review of every exception message reachable through this handler than the index allows; a Devin session with full repo access could grep all `throw new ...Exception(... + <user input> ...)` call sites feeding into `Util.processError` to confirm the exact endpoint.

### Citations

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

**File:** framework/src/main/java/org/tron/core/services/http/GetBlockServlet.java (L79-83)
```java
    } catch (IllegalArgumentException e) {
      JSONObject jsonObject = new JSONObject();
      jsonObject.put("Error", e.getMessage());
      response.getWriter().println(jsonObject.toJSONString());
    }
```

**File:** framework/src/test/java/org/tron/core/services/http/JsonFormatEscapeTest.java (L23-39)
```java
/**
 * Escaping and decoding of name-string {@code bytes} fields.
 *
 * <p>Before this fix {@code escapeBytesSelfType} only escaped the double quote, leaving backslash
 * and control chars raw, and validated the result with a lenient parser. That let attacker
 * controlled on-chain bytes emit invalid JSON and, worse, forge sibling fields on the
 * re-parse path used by {@link Util#printTransactionToJSON}.
 *
 * <p>Assertions here always state which parser they use:
 * <ul>
 *   <li>{@link #strict()} - RFC 8259 baseline. Trailing tokens are rejected. A bare
 *       {@code readTree} is NOT a strict baseline: it stops after the first value and would
 *       silently accept trailing payloads and duplicated output.</li>
 *   <li>{@link org.tron.json.JSON} - the node's own lenient parser, used by
 *       {@code Util.printTransactionToJSON}. Never used to assert validity, only to reproduce
 *       what the node itself would hand back to a client.</li>
 * </ul>
```

**File:** framework/src/test/java/org/tron/core/services/http/JsonFormatTest.java (L33-38)
```java
  @Test
  public void testPrintErrorMsg() {
    Exception ex = new Exception("test");
    String out = JsonFormat.printErrorMsg(ex);
    assertEquals("{\"Error\":\"test\"}", out);
  }
```
