The report describes a SQL injection in **PostgreSQL Anonymizer**, a PostgreSQL extension that builds masked views from security-label-derived expressions. This `Kohvert/go--001` repository is a fork of the Go language distribution itself (compiler, runtime, standard library) [1](#0-0) . It contains no masking-rule, security-label, or view-generation subsystem that could exhibit an analogous flaw — a search for such constructs found no relevant matches [2](#0-1) .

The closest conceptually related code is `database/sql` (`src/database/sql/sql.go`, `src/database/sql/convert.go`), but this package only provides a generic driver interface for parameterized queries; it does not construct SQL text, views, or expressions from user-controlled security labels/metadata the way PostgreSQL Anonymizer does, so there is no comparable sink where an attacker-supplied "masking rule" expression gets concatenated into executed SQL [3](#0-2) .

No production Go path in this repository reproduces the reported primitive (privileged expression injected into dynamically generated SQL executed with elevated privileges).

### No Vulnerability found for this question.

### Citations

**File:** doc/go_spec.html (L1-5)
```html
<!--{
	"Title": "The Go Programming Language Specification",
	"Subtitle": "Language version go1.27 (August 7, 2026)",
	"Path": "/ref/spec"
}-->
```

**File:** src/archive/tar/writer_test.go (L1-1)
```go
// Copyright 2009 The Go Authors. All rights reserved.
```

**File:** src/database/sql/sql.go (L1-1)
```go
// Copyright 2011 The Go Authors. All rights reserved.
```
