"""
The offline manual — the whole catalogue as one file that needs nothing.

Both the console and the catalogue run on pepper. A bad afternoon takes the
console *and* the instructions for fixing it, which is the one failure this
system is least able to argue its way out of. So every night the whole
catalogue is rendered into a single self-contained HTML file, committed, and
pushed with everything else. It opens from a phone, from a laptop with no
network, from a USB stick.

That requirement decides the design, and most of it is about what is left
out:

  * **No web fonts.** A Google Fonts link is a blank page on a train. The
    manual uses whatever the device already has.
  * **No JavaScript.** Nothing to fail, nothing to block, and it prints.
  * **No images from anywhere but the repository**, which the renderer
    already enforces.
  * **Record links become in-page anchors.** A link to `10.10.1.70` is worse
    than no link when 10.10.1.70 is the thing that is down.

It is a snapshot and says so, in the header and beside every measured figure.
A manual that implies it is live would be the same lie the catalogue exists
to prevent.
"""

from datetime import datetime, timezone

import citadel
import markdown

CSS = """
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:#F3F0E5;color:#102A4E;line-height:1.6;font-size:16px;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,
  "Noto Sans",sans-serif}
code,pre,.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,
  "Liberation Mono",monospace}
.wrap{max-width:860px;margin:0 auto;padding:24px 16px 72px}
header{border-bottom:2px solid #102A4E;padding-bottom:18px;margin-bottom:8px}
h1{margin:0;font-size:30px;line-height:1.15;letter-spacing:-.02em}
.lede{margin:10px 0 0;color:#39526F;font-size:15px}
.stamp{margin:12px 0 0;font-size:12px;color:#7A8BA2}
.stamp b{color:#102A4E}
.warn{margin:16px 0 0;border-left:4px solid #8A5B10;background:#F7F0DD;
  padding:10px 14px;font-size:14px;color:#5a3d0b}
.toc{margin:26px 0 0;border:1px solid #AEBCCD;background:#FCFBF6;padding:14px 18px}
.toc h2{margin:0 0 10px;font-size:12px;letter-spacing:.16em;text-transform:uppercase;
  color:#7A8BA2;font-weight:600}
.toc .grp{margin-top:12px;font-size:11px;letter-spacing:.14em;text-transform:uppercase;
  color:#7A8BA2}
.toc ul{list-style:none;margin:4px 0 0;padding:0;
  display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:1px 18px}
.toc li{margin:0;font-size:14px}
.toc a{color:#1B5BA6;text-decoration:none}
.sm{display:inline-block;font-family:ui-monospace,Menlo,Consolas,monospace;
  font-size:12px;font-weight:600;padding:1px 5px;color:#1B5BA6;background:#E7EFF9;
  border:1px solid #1B5BA6;text-decoration:none;white-space:nowrap}
.sm.q{color:#A1342A;background:none;border-style:dashed;border-color:#A1342A}
.rec{margin-top:30px;border:1px solid #AEBCCD;background:#FCFBF6;padding:18px 20px;
  page-break-inside:avoid}
.rec h2{margin:0;font-size:22px;letter-spacing:-.02em;display:flex;gap:10px;
  align-items:center;flex-wrap:wrap}
.meta{margin:10px 0 0;font-size:12px;color:#7A8BA2;
  font-family:ui-monospace,Menlo,Consolas,monospace}
.meta b{color:#39526F}
.panels{margin:12px 0 0;border:1px solid #1B5BA6;display:flex;flex-wrap:wrap}
.panels div{flex:1 1 120px;padding:7px 10px;border-right:1px solid #D8DCE2;
  background:#E7EFF9;min-width:0}
.panels div:last-child{border-right:0}
.panels span{display:block;font-size:10px;letter-spacing:.12em;text-transform:uppercase;
  color:#7A8BA2;font-family:ui-monospace,Menlo,Consolas,monospace}
.panels b{display:block;font-size:15px;font-family:ui-monospace,Menlo,Consolas,monospace}
.asat{margin:4px 0 0;font-size:11px;color:#8A5B10;
  font-family:ui-monospace,Menlo,Consolas,monospace}
.doc{margin-top:14px;font-size:15px;color:#39526F}
.doc h3{margin:18px 0 0;font-size:17px;color:#102A4E}
.doc h4{margin:14px 0 0;font-size:15px;color:#102A4E}
.doc h5{margin:12px 0 0;font-size:13px;color:#7A8BA2;text-transform:uppercase;
  letter-spacing:.08em}
.doc p{margin:7px 0 0}
.doc ul,.doc ol{margin:8px 0 0;padding-left:22px}
.doc li{margin-bottom:4px}
.doc code{background:#E7EFF9;padding:1px 4px;font-size:13px}
.doc pre{margin:10px 0 0;padding:11px 13px;background:#F3F0E5;border:1px solid #D8DCE2;
  overflow-x:auto;font-size:13px;line-height:1.6}
.doc pre code{background:none;padding:0}
.doc blockquote{margin:10px 0 0;border-left:3px solid #AEBCCD;padding-left:12px;
  color:#7A8BA2}
.doc hr{border:0;border-top:1px solid #D8DCE2;margin:16px 0 0}
.doc img{max-width:100%;height:auto}
.doc .tw{overflow-x:auto;margin-top:10px}
.doc table{border-collapse:collapse;width:100%;font-size:14px}
.doc th,.doc td{border:1px solid #D8DCE2;padding:6px 9px;text-align:left;
  vertical-align:top}
.doc th{background:#F3F0E5;font-size:11px;letter-spacing:.1em;text-transform:uppercase;
  color:#7A8BA2}
.doc .callout{margin:10px 0 0;border-left:4px solid #8A5B10;background:#F7F0DD;
  padding:9px 13px;font-size:14px}
.doc .callout>b{display:block;font-size:11px;letter-spacing:.14em;
  text-transform:uppercase;color:#8A5B10;margin-bottom:2px}
.doc .callout.danger{border-left-color:#A1342A;background:#F9EAE6}
.doc .callout.danger>b{color:#A1342A}
.doc .callout.note{border-left-color:#1B5BA6;background:#E7EFF9}
.doc .callout.note>b{color:#1B5BA6}
.empty{color:#7A8BA2;font-style:italic;font-size:14px}
.findings{margin-top:26px;border:1px solid #A1342A;background:#F9EAE6;padding:14px 18px}
.findings h2{margin:0 0 8px;font-size:14px;letter-spacing:.12em;text-transform:uppercase;
  color:#A1342A}
.findings ul{margin:0;padding-left:20px;font-size:14px;color:#5e2018}
.findings li{margin-bottom:5px}
footer{margin-top:34px;padding-top:16px;border-top:2px solid #102A4E;font-size:13px;
  color:#7A8BA2}
.top{font-size:12px;text-decoration:none;color:#1B5BA6;margin-left:auto}
@media print{
  body{background:#fff}
  .rec,.toc,.findings{border-color:#999;background:#fff}
  .top{display:none}
}
"""

GROUPS = [("host", "Hosts"), ("guest", "Guests"), ("app", "Applications"),
          ("device", "Devices"), ("runbook", "Runbooks"),
          ("register", "Registers"), ("governance", "Governance"),
          ("order", "Reading orders")]


def _esc(s):
    return markdown.esc(s)


def build(service, generated_by="the nightly job"):
    """The whole catalogue as one string of HTML.

    Reading order is deliberate and is not alphabetical: hosts first because
    everything sits on one, then guests, applications and devices, then the
    runbooks that act on them, then the registers and governance that explain
    who may. It is the order you would want if you had never seen the estate
    and something was on fire.
    """
    cat = service.cat
    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y-%m-%d %H:%M UTC")

    findings = service.findings()
    paging = [f for f in findings if f["severity"] == citadel.PAGE]

    toc, body = [], []
    total = 0

    for kind, label in GROUPS:
        recs = cat.of_kind(kind)
        if not recs:
            continue
        toc.append(f'<div class="grp">{_esc(label)}</div><ul>')
        for rec in recs:
            total += 1
            slug = citadel.slug_of(rec.shelf)
            toc.append(f'<li><a href="#{slug}">{_esc(rec.shelf)} '
                       f'{_esc(rec.title)}</a></li>')
            body.append(_record(service, rec, slug))
        toc.append("</ul>")

    warn = ""
    if service.degraded:
        warn = ('<div class="warn"><b>This was built from a cached read.</b> '
                'The document tree could not be fully read when this manual '
                'was generated, so something may be missing.</div>')

    fnd = ""
    if paging:
        items = "".join(f"<li>{_esc(f['what'])}</li>" for f in paging[:20])
        fnd = ('<div class="findings"><h2>Outstanding when this was built</h2>'
               f"<ul>{items}</ul></div>")

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Elastic Group estate manual</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
<header>
  <h1>Elastic Group estate manual</h1>
  <p class="lede">Every record in the catalogue, in one file that needs no
    network, no server and no browser extension. Open it from anywhere.</p>
  <p class="stamp">Generated <b>{_esc(stamp)}</b> by {_esc(generated_by)} &middot;
    <b>{total}</b> records &middot; <b>{len(findings)}</b> open findings
    &middot; coverage
    <b>{service.summary()['coverage']['have']} of
    {service.summary()['coverage']['total']}</b></p>
  {warn}
  <div class="warn"><b>This is a snapshot, not a live view.</b> Every measured
    figure below was true at the time above and may not be now. The living
    version is the portal; this exists for when the portal is what is
    broken.</div>
</header>
{fnd}
<div class="toc"><h2>Contents</h2>{''.join(toc)}</div>
{''.join(body)}
<footer>
  Elastic Group estate manual &middot; generated {_esc(stamp)}.
  Rebuilt nightly from the document tree and committed with it, so the copy in
  the repository is never more than a day behind the catalogue.
</footer>
</div>
</body>
</html>
"""


def _record(service, rec, slug):
    html, _ = markdown.render(rec.body, resolve=service.cat.exists,
                              media_prefix="", link_prefix="#")
    panels = citadel.panels(rec, service.store, service.portal)
    pan = ""
    if panels:
        cells = "".join(
            f"<div><span>{_esc(p['label'])}</span><b>{_esc(p['value'])}</b>"
            f"</div>" for p in panels)
        pan = (f'<div class="panels">{cells}</div>'
               '<p class="asat">measured, and only as at the time above</p>')

    over = rec.overdue
    state = ""
    if over is not None:
        word = "unverified" if rec.kind == "runbook" else "stale"
        state = f' &middot; <b>{word}</b>'

    return f"""
<section class="rec" id="{_esc(slug)}">
  <h2>{_esc(rec.title)} <span class="sm">{_esc(rec.shelf)}</span>
    <a class="top" href="#">top</a></h2>
  <p class="meta">{_esc(rec.kind)} &middot; owner <b>{_esc(rec.owner or '—')}</b>
    &middot; verified <b>{_esc(rec.verified or 'never')}</b>{state}
    &middot; {_esc(rec.path)}</p>
  {pan}
  <div class="doc">{html or '<p class="empty">Nothing written yet.</p>'}</div>
</section>
"""
