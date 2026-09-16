### Title
Unescaped exception messages in `Util.printErrorMsg` produce malformed/injectable JSON error responses on the public HTTP API - ([File: framework/src/main/java/org/tron/core/services/http/JsonFormat.java])

### Summary
`JsonFormat.printErrorMsg(Exception ex)` builds a JSON error payload by raw string concatenation of `ex.getMessage()`, with no escaping of double quotes, backslashes, or control characters. This is the same bug class as the reported Log4j 1→2 bridge issue: a structured-output serializer that fails to escape characters forbidden/special in the target format, producing malformed output that conforming parsers must reject, causing silent loss/failure on the consumer side. Here the "consumer" is any client of java-tron's public HTTP wallet API. This helper is wired into numerous unauthenticated servlets that reflect exception text (which frequently embeds the raw request input) straight back to the caller.

### Finding Description
`printErrorMsg` is defined as: [1](#0-0) 

Unlike the rest of `JsonFormat`, which carefully escapes JSON-significant characters (`escapeText`, used for every protobuf string/name field), `printErrorMsg` appends `ex.getMessage()` verbatim between two literal double quotes:
```
text.append("\"");
text.append(ex.getMessage());
text.append("\"");
```
No call to `escapeText` or any equivalent JSON escaping is made.

This function is called from many public, unauthenticated HTTP servlets to report any exception raised while handling a request, including servlets that parse raw user-controlled parameters:
- `GetTransactionInfoByBlockNumServlet` (both `doGet` and `doPost`) catches any exception from `Long.parseLong(request.getParameter("num"))` or `JsonFormat.merge(...)` and calls `Util.printErrorMsg(e)`: [2](#0-1) 
- `RateLimiterServlet` also calls `Util.printErrorMsg` directly with a constructed exception when a request is throttled: [3](#0-2) 
- Additional callers: `GetBurnTrxServlet`, `GetNodeInfoServlet`, `GetPendingSizeServlet`, `GetRewardServlet`, and `Util.java` itself, all reachable by any anonymous HTTP client.

Standard JDK exception messages routinely echo the raw, attacker-supplied input inside quotes. For example, `Long.parseLong("abc")` throws `NumberFormatException` with message:
```
For input string: "abc"
```
That message already contains an embedded, unescaped `"` character. When wrapped by `printErrorMsg`, the resulting body becomes:
```
{"Error":"For input string: "abc""}
```
This is not valid JSON: a strict RFC 8259 parser will fail on the extra unescaped quote, exactly mirroring the XML-1.0-forbidden-character scenario in the external report where a conforming parser must reject the malformed document. Because the attacker fully controls the substring embedded in the exception message (the raw query parameter), they can also embed control characters (e.g. via URL-encoded `%0A`, `%00`, etc., which many parameter parsers pass through), or, more seriously, additional double quotes and commas to attempt to inject sibling keys/values into the JSON object returned to the caller — the same "forge sibling fields" class of defect that the project's own `JsonFormatEscapeTest` regression suite explicitly documents as unacceptable for other JSON-building paths in this codebase (see comments in that test file describing exactly this risk for `escapeBytesSelfType`). `printErrorMsg`, however, was never brought into that hardening effort.

### Impact Explanation
Any unauthenticated HTTP API client can force the node to emit a malformed/attacker-influenced JSON error document merely by sending an invalid parameter (e.g. a non-numeric `num` value) to endpoints such as `/wallet/gettransactioninfobyblocknum`. Downstream consumers of the API (client SDKs, exchanges' backend integrations, monitoring/indexing systems) that use strict JSON parsers will fail to parse the response and may drop the interaction entirely or crash the calling logic — a direct integrity/availability impact on the API surface the node "can no longer serve" correctly, matching the CWE-116 bug class and the low-severity Integrity (`SI:L`) rating of the referenced advisory. The malformed output can also carry attacker-chosen quote/brace sequences into the JSON structure, which is a stronger primitive (JSON injection into the error object) than the reference advisory's log-parsing failure.

### Likelihood Explanation
High likelihood of triggering the malformed-output condition: it requires only a single anonymous HTTP GET/POST request with a syntactically invalid parameter value, no authentication, no special transaction, and no prior state. It reproduces deterministically with any input that makes a JDK exception message embed the raw attacker string (very common, e.g. `NumberFormatException`, `IllegalArgumentException` from hex decoding, etc.).

### Recommendation
Route the exception message through `JsonFormat.escapeText(...)` (the same escaping used for every other JSON string value in this class) before embedding it in the error payload:
```java
public static String printErrorMsg(Exception ex) {
  StringBuilder text = new StringBuilder();
  text.append("{\"Error\":\"");
  text.append(escapeText(ex.getMessage()));
  text.append("\"}");
  return text.toString();
}
```
Additionally audit all call sites (`GetTransactionInfoByBlockNumServlet`, `GetBurnTrxServlet`, `GetNodeInfoServlet`, `GetPendingSizeServlet`, `GetRewardServlet`, `RateLimiterServlet`, `Util.java`) to ensure none pre-format the message in a way that would double-escape or bypass the fix, and extend the existing `JsonFormatEscapeTest` suite to cover `printErrorMsg`.

### Proof of Concept
1. Send `GET /wallet/gettransactioninfobyblocknum?num=abc` to a running FullNode HTTP API (no authentication required).
2. `Long.parseLong("abc")` throws `NumberFormatException: For input string: "abc"`.
3. The servlet's catch block returns `Util.printErrorMsg(e)`, producing the HTTP body:
   ```
   {"Error":"For input string: "abc""}
   ```
4. Feed this body to any RFC-8259-conformant JSON parser (e.g. `Jackson` with default strict settings, or `JSON.parse` in a browser) — parsing fails, demonstrating the malformed/rejected structured output described by the analog bug class.

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

**File:** framework/src/main/java/org/tron/core/services/http/GetTransactionInfoByBlockNumServlet.java (L43-62)
```java
  protected void doGet(HttpServletRequest request, HttpServletResponse response) {
    try {
      boolean visible = Util.getVisible(request);
      long num = Long.parseLong(request.getParameter("num"));

      if (num > 0L) {
        TransactionInfoList reply = wallet.getTransactionInfoByBlockNum(num);
        response.getWriter().println(printTransactionInfoList(reply, visible));
      } else {
        response.getWriter().println("{}");
      }
    } catch (Exception e) {
      logger.debug("Exception: {}", e.getMessage());
      try {
        response.getWriter().println(Util.printErrorMsg(e));
      } catch (IOException ioe) {
        logger.debug("IOException: {}", ioe.getMessage());
      }
    }
  }
```

**File:** framework/src/main/java/org/tron/core/services/http/RateLimiterServlet.java (L128-136)
```java
      if (acquireResource) {
        Histogram.Timer requestTimer = Metrics.histogramStartTimer(
            MetricKeys.Histogram.HTTP_SERVICE_LATENCY, url);
        super.service(req, resp);
        Metrics.histogramObserve(requestTimer);
      } else {
        resp.getWriter()
            .println(Util.printErrorMsg(new IllegalAccessException("lack of computing resources")));
      }
```
