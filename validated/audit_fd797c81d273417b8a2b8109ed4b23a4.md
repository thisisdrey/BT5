## Title
Improper Neutralization of Substitution Characters in HTTP API Error JSON Response Construction - (File: `framework/src/main/java/org/tron/core/services/http/JsonFormat.java`)

## Summary
`JsonFormat.printErrorMsg(Exception ex)` builds the error-response JSON body used across virtually all Full-Node HTTP API servlets by directly concatenating the raw `Exception.getMessage()` string into a hand-built JSON literal, with **no escaping** of quote, backslash, or control characters:

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
``` [1](#0-0) 

This is the same bug class as the GitLab CVE-2026-9694 report: user-influenced text is substituted verbatim into a structured template (there, an email/Support-Bot template; here, a JSON response template) without neutralizing the substitution/control characters that give that template its structural meaning.

## Finding Description
`Util.processError(Exception e, HttpServletResponse response)` is the generic catch-all error handler invoked from nearly every HTTP servlet (`GetAssetIssueByIdServlet`, `GetProposalByIdServlet`, `GetBlockByLatestNumServlet`, `TransferServlet`, `GetTransactionReceiptByIdServlet`, etc.) and simply forwards to `Util.printErrorMsg(e)`:

```java
public static void processError(Exception e, HttpServletResponse response) {
    logger.debug(e.getMessage(), e);
    try {
      response.getWriter().println(Util.printErrorMsg(e));
    ...
}
``` [2](#0-1) 

Several servlets that don't go through `Util.processError` still call `Util.printErrorMsg` directly in their `catch` blocks, e.g. `GetBurnTrxServlet`, `GetPendingSizeServlet`, `GetRewardServlet`, `GetTransactionInfoByBlockNumServlet`: [3](#0-2) [4](#0-3) 

Many of these servlets parse an unauthenticated, attacker-controlled request parameter with `Long.parseLong(...)` (e.g. `num`, `id`, `offset`) before any validation of quote characters, e.g.:

```java
long num = Long.parseLong(request.getParameter("num"));
...
} catch (Exception e) {
  Util.processError(e, response);
}
``` [5](#0-4) [6](#0-5) 

`Long.parseLong`/`NumberFormatException` embeds the raw, unmodified input string inside its message (`For input string: "<raw input>"`). Because the attacker fully controls that raw input string, they control arbitrary characters — including `"`, `\`, and JSON structural characters — that end up unescaped inside `printErrorMsg`'s hand-rolled JSON.

This is precisely the class of defect that `JsonFormat`'s protobuf-field serialization path (`escapeText`, `escapeBytesSelfType`, `escapeNameStringText`) was extensively hardened against, as evidenced by the large `JsonFormatEscapeTest` regression suite documenting a prior JSON-forgery fix for protobuf field values: [7](#0-6) 

`printErrorMsg`, however, is a completely separate, unguarded code path that was never brought under that hardening — it still builds JSON via raw string concatenation.

## Impact Explanation
An anonymous HTTP API client can force any of dozens of Full-Node HTTP endpoints to return a malformed/forged JSON error body by supplying a numeric parameter value containing embedded `"` characters. The resulting response breaks out of the intended `{"Error":"..."}` structure and can inject attacker-chosen keys/values into the JSON document returned by the node's public HTTP API (e.g. `{"Error":"For input string: "x","injected":"value""}`). This can:
- Corrupt or spoof fields consumed by exchanges, wallets, explorers, or other automation that parses the node's HTTP responses as JSON, causing them to misinterpret or trust attacker-controlled content served under the node's identity — the same "impersonation/content injection" outcome flagged in the GitLab analog.
- Produce responses that some lenient parsers accept while strict parsers may reject, creating parser-differential behavior across the ecosystem of tools that talk to a java-tron full node.

This does not directly cause fund theft or consensus divergence, but it is a concrete, unauthenticated content-injection/response-forgery bug in a widely used node-facing HTTP API — matching Medium severity of the analog.

## Likelihood Explanation
Trivially reachable: any anonymous caller of the public HTTP API (e.g. `/wallet/getblockbylatestnum?num=...`, `/wallet/getproposalbyid?id=...`) can supply a crafted parameter value and immediately observe the injected/forged JSON in the response — no authentication, signature, or special privileges required.

## Recommendation
Route `Util.printErrorMsg` through the same JSON string-escaping logic already implemented and tested in `JsonFormat` (e.g. reuse `JsonFormat.escapeText(String)` on `ex.getMessage()` before concatenation), or replace the hand-built JSON string with a proper JSON serializer (e.g. Jackson `ObjectNode`, as already done in `JsonRpcServlet.buildErrorNode`) so structural characters in exception messages are always neutralized.

## Proof of Concept
1. Send an anonymous GET request to any endpoint that parses a numeric parameter and falls back to `Util.printErrorMsg`/`Util.processError` on failure, e.g.:
   `GET /wallet/getblockbylatestnum?num=1","injected":"pwned`
2. `Long.parseLong` throws `NumberFormatException` with message: `For input string: "1","injected":"pwned"`.
3. `Util.printErrorMsg` emits:
   `{"Error":"For input string: "1","injected":"pwned""}`
   which is malformed/forged JSON containing an attacker-injected `injected` key, demonstrating unauthenticated content injection into the node's HTTP API response.

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

**File:** framework/src/main/java/org/tron/core/services/http/GetPendingSizeServlet.java (L19-34)
```java
  protected void doGet(HttpServletRequest request, HttpServletResponse response) {
    try {
      long value = manager.getPendingSize();
      String out = JsonFormat.isInt64AsString()
          ? "{\"pendingSize\": \"" + value + "\"}"
          : "{\"pendingSize\": " + value + "}";
      response.getWriter().println(out);
    } catch (Exception e) {
      logger.error("", e);
      try {
        response.getWriter().println(Util.printErrorMsg(e));
      } catch (IOException ioe) {
        logger.debug("IOException: {}", ioe.getMessage());
      }
    }
  }
```

**File:** framework/src/main/java/org/tron/core/services/http/GetRewardServlet.java (L20-46)
```java
  protected void doGet(HttpServletRequest request, HttpServletResponse response) {
    try {
      long value = 0;
      byte[] address = Util.getAddress(request);
      if (address != null) {
        value = manager.getMortgageService().queryReward(address);
      }
      String out = JsonFormat.isInt64AsString()
          ? "{\"reward\": \"" + value + "\"}"
          : "{\"reward\": " + value + "}";
      response.getWriter().println(out);
    } catch (DecoderException | IllegalArgumentException e) {
      try {
        response.getWriter()
            .println("{\"Error\": " + "\"INVALID address, " + e.getMessage() + "\"}");
      } catch (IOException ioe) {
        logger.debug("IOException: {}", ioe.getMessage());
      }
    } catch (Exception e) {
      logger.error("", e);
      try {
        response.getWriter().println(Util.printErrorMsg(e));
      } catch (IOException ioe) {
        logger.debug("IOException: {}", ioe.getMessage());
      }
    }
  }
```

**File:** framework/src/main/java/org/tron/core/services/http/GetBlockByLatestNumServlet.java (L22-27)
```java
  protected void doGet(HttpServletRequest request, HttpServletResponse response) {
    try {
      fillResponse(Util.getVisible(request), Long.parseLong(request.getParameter("num")), response);
    } catch (Exception e) {
      Util.processError(e, response);
    }
```

**File:** framework/src/main/java/org/tron/core/services/http/GetProposalByIdServlet.java (L23-31)
```java
  protected void doGet(HttpServletRequest request, HttpServletResponse response) {
    try {
      boolean visible = Util.getVisible(request);
      String input = request.getParameter("id");
      long id = Long.parseLong(input);
      fillResponse(ByteString.copyFrom(ByteArray.fromLong(id)), visible, response);
    } catch (Exception e) {
      Util.processError(e, response);
    }
```

**File:** framework/src/test/java/org/tron/core/services/http/JsonFormatEscapeTest.java (L23-40)
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
 */
```
