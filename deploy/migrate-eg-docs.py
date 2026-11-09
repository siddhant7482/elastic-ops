#!/usr/bin/env python3
"""
Turn the Docusaurus knowledge base into Citadel records.

One pass, run once. It reads eg-docs/docs/**.md, converts what Docusaurus
understands into what Citadel understands, and writes an estate-docs tree.

The conversions that matter:

  * **YAML front matter becomes TOML**, and the owner and review date are
    mined out of the `:::info Page ownership` block rather than being
    invented. Those lines were maintained by hand and are the only record of
    who last looked at each page.
  * **Docusaurus admonitions become callouts.** `:::warning` is `> [!warn]`.
  * **Headings are demoted by one.** Citadel's template owns h1 and h2; a
    document that brings its own `# Title` would compete with the record
    title and produce two headings saying the same thing.
  * **Bindings are attached**, matched against the names the portal actually
    uses — `CFP / WinMan`, not `cfp-winman` — because a binding that does not
    resolve is a record describing nothing.

Deliberately NOT migrated: `operations/incidents.md`, `changes.md` and
`backup-status.md`. Those are hand-maintained copies of data the portal holds
live, and copying them into the catalogue would recreate the exact problem
this is meant to end. `infrastructure/hosts.md` goes the same way: the Hosts
index is generated.

    python3 deploy/migrate-eg-docs.py ../eg-docs out/estate-docs
"""

import argparse
import os
import re
import sys

# source path -> (kind, slug, bind, title override)
PLAN = {
    # source path: (kind, slug, SHELF, bind, title override)
    #
    # The shelf mark is written here rather than derived. Derivation takes the
    # first three letters of the slug, which turns restore-cfp, restore-testing,
    # restore-workstation and restore-arrearsman into four records all claiming
    # RUN.RES — three of them silently lost. A shelf mark is a name people say
    # out loud; names get chosen, not computed.
    "infrastructure/host-pepper.md":   ("host", "pepper", "HST.PEP", {"host": "pepper"}, "pepper"),
    "infrastructure/host-proxmox.md":  ("host", "proxmox", "HST.PRX", {"host": "proxmox"}, "proxmox"),
    "infrastructure/host-cinnamon.md": ("host", "cinnamon", "HST.CIN", {"host": "cinnamon"}, "cinnamon"),
    "infrastructure/host-yam.md":      ("host", "yam", "HST.YAM", {"host": "yam"}, "yam"),
    "infrastructure/network.md":       ("register", "network", "REG.NET", {}, "Network and addressing"),
    "infrastructure/storage.md":       ("register", "storage", "REG.STO", {}, "Storage"),

    "systems/cfp-winman.md":        ("app", "winman", "APP.WIN", {"app": "CFP / WinMan"}, "CFP / WinMan"),
    "systems/arrearsman.md":        ("app", "arrearsman", "APP.ARM", {"app": "ArrearsMan"}, "ArrearsMan"),
    "systems/vaultwarden.md":       ("app", "vaultwarden", "APP.VWD", {"app": "Vaultwarden"}, "Vaultwarden"),
    "systems/monitoring-zabbix.md": ("app", "zabbix", "APP.ZBX", {"app": "Zabbix"}, "Zabbix"),
    "systems/bigquery-export.md":   ("app", "bigquery-export", "APP.BQX", {}, "BigQuery export"),

    "runbooks/restore-cfp.md":           ("runbook", "restore-cfp", "RUN.CFP", {"records": ["APP.WIN", "HST.CIN"]}, None),
    "runbooks/restore-arrearsman.md":    ("runbook", "restore-arrearsman", "RUN.ARM", {"records": ["APP.ARM", "HST.PRX"]}, None),
    "runbooks/restore-workstation.md":   ("runbook", "restore-workstation", "RUN.WKS", {"records": ["HST.PRX"]}, None),
    "runbooks/restore-testing.md":       ("runbook", "restore-testing", "RUN.DRL", {}, None),
    "runbooks/pbs-operations.md":        ("runbook", "pbs-operations", "RUN.PBS", {}, None),
    "runbooks/winman-client-fix.md":     ("runbook", "winman-client-fix", "RUN.WMC", {"records": ["APP.WIN"]}, None),
    "runbooks/arrears-login-fix.md":     ("runbook", "arrears-login-fix", "RUN.ALF", {"records": ["APP.ARM"]}, None),
    "runbooks/vaultwarden-users.md":     ("runbook", "vaultwarden-users", "RUN.VWU", {"records": ["APP.VWD"]}, None),
    "runbooks/isolated-test-network.md": ("runbook", "isolated-test-network", "RUN.ISO", {}, None),
    "runbooks/network-relay-setup.md":   ("runbook", "network-relay-setup", "RUN.RLY", {}, None),

    "registers/credentials-index.md": ("register", "credentials", "REG.CRED", {}, "Credentials index"),
    "registers/asset-vm.md":          ("register", "asset-vm", "REG.VM", {}, "Asset register"),
    "registers/network-ip.md":        ("register", "network-ip", "REG.IP", {}, "Network and IP register"),
    "registers/backup-recovery.md":   ("register", "backup-recovery", "REG.BKP", {}, "Backup and recovery register"),

    "governance/roles-access.md":           ("governance", "roles-access", "GOV.ROLE", {}, "Roles and access"),
    "governance/onboarding-offboarding.md": ("governance", "onboarding", "GOV.ONB", {}, "Onboarding and offboarding"),
    "governance/doc-standards.md":          ("governance", "doc-standards", "GOV.STD", {}, "Documentation standards"),
    "operations/reviews.md":                ("governance", "reviews", "GOV.REV", {}, "Review schedule"),
    "intro.md":                             ("order", "taking-over", "ORD.NEW", {}, "Taking over the estate"),
}

# Live data in the portal already. Copying them in would recreate the problem.
SKIP = {
    "operations/incidents.md": "the portal's incident log is live",
    "operations/changes.md": "the portal's change log is live",
    "operations/backup-status.md": "the backup panel is live",
    "infrastructure/hosts.md": "the Hosts index is generated",
}

ADMONITION = {"info": "note", "note": "note", "tip": "note",
              "warning": "warn", "caution": "warn", "danger": "danger"}

_YAML = re.compile(r"^---\n(.*?)\n---\n", re.S)
_OWNER = re.compile(r"\*\*Owner:\*\*\s*([^·|\n]+)")
_REVIEWED = re.compile(r"\*\*Reviewed:\*\*\s*(\d{4}-\d{2}-\d{2})")
_NEXT = re.compile(r"\*\*Next review:\*\*\s*(\d{4}-\d{2}-\d{2})")
_TITLE = re.compile(r'^title:\s*"?(.*?)"?\s*$', re.M)


def toml_str(v):
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'


def convert(text):
    """Docusaurus markdown -> the Citadel subset. Returns (body, meta)."""
    meta = {}
    m = _YAML.match(text)
    if m:
        t = _TITLE.search(m.group(1))
        if t:
            meta["title"] = t.group(1).strip()
        text = text[m.end():]

    for pat, key in ((_OWNER, "owner"), (_REVIEWED, "verified"),
                     (_NEXT, "next")):
        hit = pat.search(text)
        if hit:
            meta[key] = hit.group(1).strip()

    out, i = [], 0
    lines = text.split("\n")
    while i < len(lines):
        line = lines[i]

        # :::kind Title ... :::  ->  > [!kind] Title ...
        a = re.match(r"^:::\s*(\w+)\s*(.*)$", line)
        if a and a.group(1).lower() in ADMONITION:
            kind = ADMONITION[a.group(1).lower()]
            buf = [a.group(2).strip()] if a.group(2).strip() else []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith(":::"):
                if lines[i].strip():
                    buf.append(lines[i].strip())
                i += 1
            i += 1
            body = " ".join(buf).strip()
            if body:
                out.append(f"> [!{kind}] {body}")
                out.append("")
            continue

        if line.strip().startswith(":::"):      # stray closer
            i += 1
            continue

        # demote headings: the record template owns h1 and h2
        h = re.match(r"^(#{1,5})\s+(.*)$", line)
        if h:
            out.append("#" * min(len(h.group(1)) + 1, 5) + " " + h.group(2))
            i += 1
            continue

        # Docusaurus link syntax that Citadel will not follow
        line = re.sub(r"\]\((?!https?://)[^)]*\)", "]", line)
        out.append(line)
        i += 1

    body = "\n".join(out)
    body = re.sub(r"\n{3,}", "\n\n", body).strip() + "\n"
    return body, meta


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("source", help="the eg-docs checkout")
    ap.add_argument("dest", help="where to write the estate-docs tree")
    ap.add_argument("--owner", default="siddhant")
    args = ap.parse_args()

    docs = os.path.join(args.source, "docs")
    if not os.path.isdir(docs):
        print(f"no docs/ under {args.source}", file=sys.stderr)
        return 2

    seen, written = set(), 0
    for rel, (kind, slug, shelf, bind, title) in sorted(PLAN.items()):
        src = os.path.join(docs, rel.replace("/", os.sep))
        if not os.path.isfile(src):
            print(f"  --   missing: {rel}")
            continue
        seen.add(rel)
        with open(src, encoding="utf-8") as fh:
            body, meta = convert(fh.read())

        title = title or meta.get("title") or slug
        title = re.sub(r"^(Host|Runbook|Register)\s*[—–-]\s*", "", title)
        title = re.sub(r"\s*\([^)]*\)\s*$", "", title).strip()

        owner = (meta.get("owner") or args.owner).strip().lower().split()[0]
        verified = meta.get("verified") or ""

        fm = [f"title    = {toml_str(title)}",
              f"kind     = {toml_str(kind)}",
              f"shelf    = {toml_str(shelf)}",
              f"owner    = {toml_str(owner)}"]
        if verified:
            fm.append(f"verified = {toml_str(verified)}")
        fm.append('review   = "6 months"')
        if bind:
            fm.append("")
            fm.append("[bind]")
            for k, v in bind.items():
                if isinstance(v, list):
                    fm.append(f"{k} = [" + ", ".join(toml_str(x) for x in v) + "]")
                elif isinstance(v, bool):
                    fm.append(f"{k} = {'true' if v else 'false'}")
                else:
                    fm.append(f"{k} = {toml_str(v)}")
        if kind == "host":
            fm.append("guests = true")
            fm.append("backups = true")

        out = os.path.join(args.dest, kind, slug + ".md")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("+++\n" + "\n".join(fm) + "\n+++\n\n" + body)
        written += 1
        print(f"  ok   {kind}/{slug}.md   <- {rel}"
              + ("" if verified else "   (no review date found)"))

    # Records that were never in the knowledge base: things learned since, or
    # things it got wrong. Copied verbatim — they are already in the right
    # shape and are version-controlled alongside this script.
    seed = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "seed-records")
    if os.path.isdir(seed):
        print()
        for dirpath, _, names in os.walk(seed):
            for n in sorted(names):
                if not n.endswith(".md"):
                    continue
                src = os.path.join(dirpath, n)
                rel = os.path.relpath(src, seed).replace(os.sep, "/")
                dst = os.path.join(args.dest, rel.replace("/", os.sep))
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                with open(src, encoding="utf-8") as fh:
                    body = fh.read()
                with open(dst, "w", encoding="utf-8", newline="\n") as fh:
                    fh.write(body)
                written += 1
                print(f"  ok   {rel}   <- seed")

    print()
    for rel, why in sorted(SKIP.items()):
        print(f"  skip {rel}  — {why}")

    found = set()
    for dirpath, _, names in os.walk(docs):
        for n in names:
            if n.endswith(".md"):
                p = os.path.relpath(os.path.join(dirpath, n), docs)
                found.add(p.replace(os.sep, "/"))
    unplanned = sorted(found - seen - set(SKIP))
    if unplanned:
        print("\nNot in the plan, so not migrated:")
        for p in unplanned:
            print(f"  ?    {p}")

    print(f"\n{written} records written to {args.dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
