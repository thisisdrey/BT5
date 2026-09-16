### Title
Reflected HTML/JSON-breaking injection via unescaped exception message in Solidity transaction lookup servlets - (File: `framework/src/main/java/org/tron/core/services/http/solidity/GetTransactionByIdSolidityServlet.java`)

### Summary
`GetTransactionByIdSolidityServlet` and `GetTransactionInfoByIdSolidityServlet` catch exceptions from malformed `value` input (hex decode failures) and write `e.getMessage()` directly to the HTTP response with `response.getWriter().println(e.getMessage())`, with no JSON escaping and no explicit `text/plain`/`application/json` content type set on that error path.

### Finding Description
In the normal (success) path these servlets build a proper JSON body via `JsonFormat.printToString`/`Util.printTransaction`, which route through `JsonFormat.escapeText` (see `framework/src/main/java/org/tron/core/services/http/JsonFormat.java:934`), correctly escaping quotes, backslashes, and control characters. However, in the `catch (Exception e)` branches of both servlets, the raw exception message is written unescaped: [1](#0-0) [2](#0-1) 

The `value` request parameter feeds directly into `ByteArray.fromHexString(input)`; a malformed hex string (or a string containing HTML-significant characters such as `<`, `>`, `"`) triggers a `NumberFormatException`/`IllegalArgumentException` whose message frequently echoes the offending input verbatim. That message is written back into the HTTP body with no escaping, and the servlet does not call `response.setContentType(...)` on this branch, unlike the JSON success path.

Separately, `JsonFormat.printErrorMsg(Exception ex)` (as opposed to the properly-escaping `Util.printErrorMsg`) also builds a JSON body via raw `StringBuilder` concatenation of `ex.getMessage()` with no escaping at all: [3](#0-2) 

This is the same bug class as CVE-2020-8821: attacker-controlled input is echoed back through an HTTP endpoint into a rendered surface without escaping/sanitization, producing HTML injection when a client (or any tooling/dashboard) renders the response as HTML rather than strict JSON.

### Impact Explanation
This is a reflected content-injection issue reachable by any unauthenticated caller of the public full-node HTTP API (`/walletsolidity/gettransactionbyid`, `/walletsolidity/gettransactioninfobyid`) simply by submitting a crafted `value` parameter. Impact is limited to whatever renders the raw response as HTML (e.g., a browser-based API explorer, a wallet's debug console, or a proxy that assumes JSON output is safe to display); it does not itself achieve JavaScript execution, matching the CVE's own severity characterization ("HTML rendered ... JavaScript is not executed"). No funds, keys, or consensus state are affected; this is a data-validation/output-encoding gap on error paths of two read-only query servlets.

### Likelihood Explanation
High likelihood of triggering (any malformed hex value reaches the vulnerable catch block), but low-severity outcome since exploitation requires a downstream HTML-rendering consumer of the JSON API and does not by itself execute script. This keeps the analog at Medium severity, consistent with the CVSS 5.4 rating of the original CVE.

### Recommendation
Route all error responses in these servlets (and `JsonFormat.printErrorMsg`) through the same escaping used for normal responses — e.g. call `Util.printErrorMsg(e)` (which builds the JSON via `JSONObject`/fastjson and correctly escapes) instead of directly writing `e.getMessage()`, and explicitly set `response.setContentType("application/json; charset=utf-8")` on every response path, success or error, so browser-based clients never interpret the body as HTML.

### Proof of Concept
1. Send `GET /walletsolidity/gettransactionbyid?value=<script>alert(1)</script>` (or any non-hex string containing `<`/`>`/`"`) to a java-tron full node's HTTP API.
2. `ByteArray.fromHexString` throws, and its exception message (containing the raw attacker string) is written unescaped via `response.getWriter().println(e.getMessage())` with no `Content-Type` override.
3. If the response is rendered in a browser or any HTML-capable client (e.g., a debugging tool that displays raw API responses), the injected markup renders as HTML in the page, analogous to the Webmin Action Log rendering described in CVE-2020-8821.

### Citations

**File:** framework/src/main/java/org/tron/core/services/http/solidity/GetTransactionByIdSolidityServlet.java (L32-39)
```java
    } catch (Exception e) {
      logger.debug("Exception: {}", e.getMessage());
      try {
        response.getWriter().println(e.getMessage());
      } catch (IOException ioe) {
        logger.debug("IOException: {}", ioe.getMessage());
      }
    }
```

**File:** framework/src/main/java/org/tron/core/services/http/solidity/GetTransactionInfoByIdSolidityServlet.java (L39-46)
```java
    } catch (Exception e) {
      logger.debug("Exception: {}", e.getMessage());
      try {
        response.getWriter().println(e.getMessage());
      } catch (IOException ioe) {
        logger.debug("IOException: {}", ioe.getMessage());
      }
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
