#!/usr/bin/env python3
"""Build the GitHub release page for wsprpool.

    python scripts/release_notes.py v1.2.0 dist --prev v1.1.0 --out body.md

dist/ holds the files that go on the release. This adds a web-seeded .torrent for each
one, a QR code of each magnet link when segno is installed, and SHA256SUMS.txt over
everything, then writes the page body to --out and its title to title.txt beside it.
It exits 1 when CHANGELOG.md has no section for the version, since a failed release
beats one with empty notes. The section is copied verbatim rather than through
`patchnotes show`, whose indented bullets render as a code block.
"""

import argparse
import hashlib
import os
import re
import sys
import time
import urllib.parse

try:
    import segno  # optional: QR codes for the magnet links
except ImportError:
    segno = None

APP = "wsprpool"
REPO = "Londopy/wsprpool"
INTRO = (
    "**wsprpool** decodes WSPR spots that no single receiver can hear, by recording several "
    "KiwiSDRs at once and adding their soft bits before the decoder. "
    "See the [README](https://github.com/{repo}#readme) for setup."
)

# (regex on the file name, who it's for, notes). first match wins.
KINDS = [
    (r"-py3-none-any\.whl$", "Python 3.11+, any OS", "**wheel (recommended)**, install with pip"),
    (r"\.tar\.gz$", "Python 3.11+, any OS", "source distribution"),
]
# in SHA256SUMS.txt, but not rows of their own in the download table
HIDDEN = re.compile(r"(\.torrent|\.magnet\.png|^SHA256SUMS\.txt)$")
NO_TORRENT = re.compile(r"(\.torrent|\.png|\.txt)$")

# public trackers help magnet links find peers; the web seed covers no-peer cases
TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.torrent.eu.org:451/announce",
]
PIECE = 1 << 20  # 1 MiB pieces


def bencode(x):
    if isinstance(x, bool):
        raise TypeError("no booleans in bencoding")
    if isinstance(x, int):
        return b"i%de" % x
    if isinstance(x, str):
        x = x.encode()
    if isinstance(x, bytes):
        return b"%d:" % len(x) + x
    if isinstance(x, list):
        return b"l" + b"".join(bencode(i) for i in x) + b"e"
    if isinstance(x, dict):
        return b"d" + b"".join(bencode(k) + bencode(x[k]) for k in sorted(x)) + b"e"
    raise TypeError(type(x))


def make_torrent(path, name, url, comment):
    """Single-file torrent with `url` as its web seed. Returns (bytes, infohash, size).

    One torrent per file on purpose: GitHub redirects every download to its
    CDN, and per-file redirects only work cleanly when each torrent is one file.
    """
    pieces, size = [], 0
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(PIECE), b""):
            pieces.append(hashlib.sha1(chunk).digest())
            size += len(chunk)
    info = {"name": name, "length": size, "piece length": PIECE, "pieces": b"".join(pieces)}
    infohash = hashlib.sha1(bencode(info)).hexdigest()
    meta = {
        "info": info,
        "url-list": [url],
        "announce": TRACKERS[0],
        "announce-list": [[t] for t in TRACKERS],
        "creation date": int(time.time()),
        "created by": "scripts/release_notes.py",
        "comment": comment,
    }
    return bencode(meta), infohash, size


def magnet_link(infohash, name, size, url):
    parts = [f"xt=urn:btih:{infohash}", f"dn={urllib.parse.quote(name)}", f"xl={size}"]
    parts += [f"tr={urllib.parse.quote(t, safe='')}" for t in TRACKERS]
    parts.append(f"ws={urllib.parse.quote(url, safe='')}")
    return "magnet:?" + "&".join(parts)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def kind(name):
    """(table position, who it's for, notes) for a file name, wheel first."""
    for i, (pattern, label, note) in enumerate(KINDS):
        if re.search(pattern, name):
            return i, label, note
    return len(KINDS), "Other", ""


HEADING = re.compile(r"^## +\[?([^\]\s]+)\]?[^\n]*$", re.M)


def changelog_section(text, version):
    """(date, release name, why, body) for `## [version]`, or None. Body is verbatim."""
    matches = list(HEADING.finditer(text))
    for i, m in enumerate(matches):
        if m.group(1).lstrip("vV") != version:
            continue
        date_m = re.search(r"\d{4}-\d{2}-\d{2}", m.group(0))
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[m.end() : end]
        # drop the compare-link footnotes that end a Keep a Changelog file,
        # and a trailing "---" separator
        body = re.sub(r"^\[[^\]]+\]:\s*\S+\s*$", "", body, flags=re.M)
        body = re.sub(r"\n-{3,}\s*$", "", body.rstrip()).strip("\n")
        # optional release name on the first line: *Name* or *Name* - why
        name = why = ""
        nm = re.match(r"\s*\*([^*\n]+)\*(?:\s+[—-]+\s+(.*))?\s*(?:\n|$)", body)
        if nm:
            name, why = nm.group(1).strip(), (nm.group(2) or "").strip()
            body = body[nm.end() :].strip("\n")
        return (date_m.group(0) if date_m else ""), name, why, body.strip()
    return None


def main():
    ap = argparse.ArgumentParser(description="Build the release page")
    ap.add_argument("tag")
    ap.add_argument("assets")
    ap.add_argument("--prev", default="", help="previous tag, for the compare link")
    ap.add_argument("--changelog", default="CHANGELOG.md")
    ap.add_argument("--out", default="body.md")
    ap.add_argument("--repo", default=REPO)
    a = ap.parse_args()
    tag, version = a.tag, a.tag.lstrip("vV")
    # asset links must be absolute: a relative link in a release body resolves
    # under /releases/tag/ and 404s
    download = f"https://github.com/{a.repo}/releases/download/{tag}"

    section = changelog_section(open(a.changelog, encoding="utf-8").read(), version)
    if section is None:
        print(f"release_notes: {a.changelog} has no section for {version}", file=sys.stderr)
        return 1
    date, rel_name, why, changes = section

    def files():
        return sorted(
            f
            for f in os.listdir(a.assets)
            if os.path.isfile(os.path.join(a.assets, f)) and f != "SHA256SUMS.txt"
        )

    torrents = []  # (file, torrent file, magnet, qr file or "")
    for f in files():
        if NO_TORRENT.search(f):
            continue
        url = f"{download}/{f}"
        data, infohash, size = make_torrent(
            os.path.join(a.assets, f), f, url, f"{APP} {tag}: {url} is the web seed"
        )
        tfile = f + ".torrent"
        with open(os.path.join(a.assets, tfile), "wb") as fh:
            fh.write(data)
        magnet = magnet_link(infohash, f, size, url)
        qr = ""
        if segno is not None:
            qr = f + ".magnet.png"
            segno.make(magnet, error="m").save(os.path.join(a.assets, qr), scale=3, border=2)
        torrents.append((f, tfile, magnet, qr))

    # checksums for everything, torrents and QR codes included
    sums = [
        (f, sha256(os.path.join(a.assets, f)), os.path.getsize(os.path.join(a.assets, f)))
        for f in files()
    ]
    with open(os.path.join(a.assets, "SHA256SUMS.txt"), "w", encoding="utf-8", newline="\n") as fh:
        for f, digest, _ in sums:
            fh.write(f"{digest}  {f}\n")

    wheel = next((f for f, _, _ in sums if f.endswith(".whl")), None)
    if wheel is None:
        print(f"release_notes: no wheel in {a.assets}", file=sys.stderr)
        return 1

    title = f"{APP} {tag}" + (f" — {rel_name}" if rel_name else "")
    out = []
    if rel_name:
        out += [f"*{rel_name}* — {why}" if why else f"*{rel_name}*", ""]
    out += [INTRO.format(repo=a.repo), ""]
    out += [f"## What's new in {version}" + (f" ({date})" if date else ""), "", changes, ""]

    out += ["## Downloads", "", "| File | For | Size | Notes |", "|---|---|---|---|"]
    for f, _, size in sorted(sums, key=lambda row: (kind(row[0])[0], row[0])):
        if HIDDEN.search(f):
            continue
        _, label, note = kind(f)
        out.append(f"| [`{f}`]({download}/{f}) | {label} | {human(size)} | {note} |")
    out += [""]

    out += [
        "## Install",
        "",
        "With Python 3.11 or newer, install the wheel straight from this release:",
        "",
        "```console",
        f"pip install {download}/{wheel}",
        "```",
        "",
        "`wsprpool sim` and `wsprpool decode` work right away. `wsprpool record` also needs "
        "`kiwirecorder.py` from [kiwiclient](https://github.com/jks-prv/kiwiclient); the "
        f"[README](https://github.com/{a.repo}#install) says how to point wsprpool at it.",
        "",
    ]

    out += [
        "## Verify a download",
        "",
        "`SHA256SUMS.txt` lists the SHA-256 of every file on this release. In PowerShell, "
        "next to the download:",
        "",
        "```powershell",
        f"(Get-FileHash .\\{wheel} -Algorithm SHA256).Hash.ToLower()",
        "```",
        "",
        "and compare it with that file's line below. On Linux: "
        "`sha256sum -c SHA256SUMS.txt --ignore-missing`; on macOS: "
        "`shasum -a 256 -c SHA256SUMS.txt --ignore-missing`.",
        "",
        "<details><summary>Checksums</summary>",
        "",
        "```",
    ]
    out += [f"{digest}  {f}" for f, digest, _ in sums]
    out += ["```", "", "</details>", ""]

    if torrents:
        out += [
            "## Torrents",
            "",
            "Every download is also a `.torrent` with its GitHub download as the web seed: a "
            "BitTorrent client gets it from GitHub when nobody is seeding, and from peers when "
            "they are. The magnet links carry the same web seed, but need one peer to fetch the "
            "file list first.",
            "",
            "| File | Torrent | Magnet (QR) |",
            "|---|---|---|",
        ]
        torrents.sort(key=lambda t: (kind(t[0])[0], t[0]))
        for f, tfile, magnet, qr in torrents:
            cell = (
                f'<img src="{download}/{qr}" width="220" alt="QR code of the magnet link for {f}">'
                if qr
                else ""
            )
            out.append(f"| `{f}` | [`{tfile}`]({download}/{tfile}) | {cell} |")
        out += ["", "<details><summary>Magnet links</summary>", "", "```"]
        for f, _, magnet, _ in torrents:
            out += [f"# {f}", magnet, ""]
        out += ["```", "", "</details>", ""]

    out += ["---", ""]
    if a.prev:
        out.append(f"**Full changelog:** https://github.com/{a.repo}/compare/{a.prev}...{tag}")
    else:
        out.append(f"**Full changelog:** https://github.com/{a.repo}/commits/{tag}")
    out += [
        "",
        f"[README](https://github.com/{a.repo}#readme) · "
        f"[Changelog](https://github.com/{a.repo}/blob/main/CHANGELOG.md) · "
        f"[Report a problem](https://github.com/{a.repo}/issues)",
    ]

    with open(a.out, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(out) + "\n")
    title_path = os.path.join(os.path.dirname(os.path.abspath(a.out)), "title.txt")
    with open(title_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(title + "\n")
    print(
        f"wrote {a.out}, title.txt and SHA256SUMS.txt ({len(sums)} files, {len(torrents)} torrents"
        + ("" if segno else ", no QR codes: segno not installed")
        + ")"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
