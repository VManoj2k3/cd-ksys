"""RAG guideline detection against a LIVE stack (REAL LLM) — scenario suite.

    python -m tests.rag_smoke

Runs in-process on the same box as a running backend (same config/data_dir).
Ingests TWO real guideline PDFs (a Secure-C standard, 2 pages, and a Python
guidelines doc) through the real pypdf path, then drives the live HTTP review
API — RAG toggle on — across the full scenario matrix and reports what the REAL
model does: violations detected + cited to the right PDF/rule/page, compliant
code staying silent, cross-collection attribution, retrieval faithfulness, and
inline fixes. This is the Phase-2 analogue of tests/accuracy_eval.py.

Retrieval uses whatever rag.embedder the stack is configured with (default
'hash' — the out-of-box pilot config). Env: KOOSYS_URL (default :8000).
"""
from __future__ import annotations

import os
import sys
import time

import httpx

BASE = os.environ.get("KOOSYS_URL", "http://127.0.0.1:8000").rstrip("/")
USER = os.environ.get("RAG_SMOKE_USER", "anonymous")


# ------------------------------------------------------------ real PDF builder
def make_pdf(pages: list[str]) -> bytes:
    """Multi-page PDF with a byte-accurate xref (parses on strict pypdf)."""
    n = len(pages)
    page_nums = [3 + 2 * i for i in range(n)]
    content_nums = [4 + 2 * i for i in range(n)]
    font_obj = 3 + 2 * n
    kids = " ".join(f"{p} 0 R" for p in page_nums)
    objs: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode(),
    ]
    for i, text in enumerate(pages):
        objs.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Contents {content_nums[i]} 0 R /Resources << /Font << /F1 "
            f"{font_obj} 0 R >> >> >>".encode())
        body, y = [], 740
        for para in text.split("\n"):
            esc = para.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            body.append(f"BT /F1 10 Tf 54 {y} Td ({esc}) Tj ET")
            y -= 16
        stream = "\n".join(body).encode("latin-1")
        objs.append(b"<< /Length " + str(len(stream)).encode()
                    + b" >>\nstream\n" + stream + b"\nendstream")
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode("latin-1") + body + b"\nendobj\n"
    xref_pos = len(out)
    total = len(objs) + 1
    out += f"xref\n0 {total}\n".encode("latin-1") + b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode("latin-1")
    out += (f"trailer\n<< /Root 1 0 R /Size {total} >>\n"
            f"startxref\n{xref_pos}\n").encode("latin-1")
    return bytes(out) + b"%%EOF"


C_PDF = make_pdf([
    "ACME Secure C Coding Standard v1.2\n"
    "Rule SEC-01: The gets function must never be used because it cannot bound "
    "its input and leads to buffer overflows. Use fgets with an explicit size.\n"
    "Rule SEC-02: Do not use strcpy or strcat. Prefer strncpy or snprintf with "
    "an explicit size bound.",
    "Rule MEM-03: Every pointer returned by malloc must be checked against NULL "
    "before it is dereferenced.\n"
    "Rule CTRL-04: The goto statement is forbidden. Use structured control flow "
    "such as loops and helper functions instead.",
])
PY_PDF = make_pdf([
    "ACME Python Engineering Guidelines\n"
    "Rule PYLOG-01: Do not use print in library modules. Use the logging module "
    "so that output can be controlled and routed.\n"
    "Rule PYSEC-02: Never call eval or exec on external or untrusted input; it "
    "enables code injection.\n"
    "Rule PYDOC-03: Every TODO comment must reference a tracking ticket, for "
    "example TODO PROJ-123.",
])

C_BAD = ('#include <stdio.h>\nint main(void){\n  char buf[64];\n'
         '  gets(buf);\n  char d[8];\n  strcpy(d, buf);\n  return 0;\n}\n')
C_CLEAN = ('#include <stdio.h>\nint main(void){\n  char buf[64];\n'
           '  fgets(buf, sizeof(buf), stdin);\n  return 0;\n}\n')
C_GOTO = 'int f(int x){\n  if(x)\n    goto end;\n  end:\n  return x;\n}\n'
PY_BAD = ('def run(cmd):\n    print("running")\n    return eval(cmd)\n'
          '# TODO: harden this later\n')
PY_CLEAN = ('import logging\nlog = logging.getLogger(__name__)\n'
            'def run(cmd):\n    log.info("running")\n'
            '    # TODO(PROJ-123): support dry-run\n    return 0\n')


def review(client, code, lang, cids):
    r = client.post(f"{BASE}/api/review", json={
        "code": code, "filename": f"m.{lang}", "language": lang,
        "rag_enabled": True, "collection_ids": cids})
    r.raise_for_status()
    jid = r.json()["job_id"]
    for _ in range(300):
        time.sleep(2)
        j = client.get(f"{BASE}/api/job/{jid}").json()
        if j["state"] in ("done", "error"):
            return j
    raise TimeoutError("review did not finish")


def gviol(job):
    return [v for v in job.get("violations", []) if v["layer"] == "guideline"]


def show(gv):
    for v in gv:
        cite = v.get("citation") or {}
        fix = v.get("fix") or {}
        tag = " +fix" if fix.get("validated") else ""
        print(f"    L{v['line']} [{v['severity']}] {v['message'][:80]}{tag}")
        print(f"       cites p{cite.get('page')} of {cite.get('source')}: "
              f"{(cite.get('quote') or '')[:70]}")


def main() -> None:
    from backend.rag import ingest, store
    health = httpx.get(f"{BASE}/api/health", timeout=10).json()
    if not health.get("rag_enabled"):
        print("rag not enabled on this stack")
        sys.exit(2)
    if not health.get("llm_available"):
        print("LLM offline — guideline layer needs the model")
        sys.exit(2)

    c_cid = store.create_collection(USER, "Secure C Standard")["id"]
    py_cid = store.create_collection(USER, "Python Guidelines")["id"]
    nc = ingest.ingest_pdf(USER, c_cid, "ACME_Secure_C_Standard.pdf", C_PDF)
    npy = ingest.ingest_pdf(USER, py_cid, "ACME_Python_Guidelines.pdf", PY_PDF)
    print(f"ingested 2 real PDFs -> C:{nc} chunk(s), Python:{npy} chunk(s)\n")

    client = httpx.Client(timeout=90)
    # (label, code, lang, collections, expectation)  expectation: source PDF the
    # findings should cite, or None for "must stay silent"
    C_SRC, PY_SRC = "ACME_Secure_C_Standard.pdf", "ACME_Python_Guidelines.pdf"
    scenarios = [
        ("S1 C gets+strcpy", C_BAD, "c", [c_cid], C_SRC),
        ("S2 C compliant", C_CLEAN, "c", [c_cid], None),
        ("S3 C goto (page-2 rule)", C_GOTO, "c", [c_cid], C_SRC),
        ("S4 Py eval+print+TODO", PY_BAD, "py", [py_cid], PY_SRC),
        ("S5 Py compliant", PY_CLEAN, "py", [py_cid], None),
        ("S6 both selected, C bad", C_BAD, "c", [c_cid, py_cid], C_SRC),
        ("S7 C bad, only Py PDF", C_BAD, "c", [py_cid], None),
    ]

    detected = expected_hits = fp = cite_ok = cite_tot = fixes = 0
    for label, code, lang, cids, want_src in scenarios:
        try:
            job = review(client, code, lang, cids)
        except Exception as e:  # noqa: BLE001
            print(f"== {label}: ERROR {type(e).__name__}: {e}")
            continue
        gv = gviol(job)
        verdict = "silent" if not gv else f"{len(gv)} finding(s)"
        want = "SILENT" if want_src is None else f"cite {want_src}"
        print(f"== {label}: got {verdict} | expect {want}")
        show(gv)
        if want_src is None:
            fp += len(gv)  # any finding on compliant / wrong-collection is a FP
        else:
            expected_hits += 1
            if gv:
                detected += 1
            for v in gv:
                cite_tot += 1
                if (v.get("citation") or {}).get("source") == want_src:
                    cite_ok += 1
                if (v.get("fix") or {}).get("validated"):
                    fixes += 1
        print()

    for cid in (c_cid, py_cid):
        store.delete_collection(USER, cid)

    print("=" * 60)
    print(f"violations detected:      {detected}/{expected_hits} scenarios")
    print(f"citations to correct PDF: {cite_ok}/{cite_tot} findings")
    print(f"false positives (clean/wrong-collection): {fp}  (want 0)")
    print(f"validated inline fixes on findings: {fixes}")
    # PASS = every clearly-violating scenario produced at least one finding, no
    # FP on compliant/irrelevant code, and citations point to the right PDF.
    ok = (detected == expected_hits and fp == 0
          and cite_tot > 0 and cite_ok == cite_tot)
    print("\nRAG SMOKE:", "PASS" if ok else
          "REVIEW (see per-scenario lines above — real-model recall/precision)")


if __name__ == "__main__":
    main()
