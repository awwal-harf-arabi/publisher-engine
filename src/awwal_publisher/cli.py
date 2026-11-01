"""Editorial-gated publishing CLI for multilingual, multi-volume novels.

Safety contract:
- Source manuscripts are read-only; normalized files are disposable.
- A PREVIEW/PROOF is never labeled READY FOR SALE.
- A RELEASE build must pass all explicit gates.
- Kindle output is an EPUB3 for KDP upload, not a fabricated KPF.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from PIL import Image

VERSION = "0.1.0"
FORMATS = {"epub", "docx", "html", "digital-pdf", "print-pdf"}
APPROVED = {"approved", "locked"}
READABLE = {"review", "approved", "locked"}
LANGUAGE_RE = re.compile(r"^[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")
FRONT_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
CALLOUT_RE = re.compile(r"(?m)^\s*>\s*\[!(?:warning|todo|danger|question|note|abstract)\]", re.I)
PLACEHOLDER_RE = re.compile(r"\[\s*(?:TODO|TBD|النص الروائي|عنوان مؤقت)\s*\]", re.I)
WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
RTL_BASE = {"ar", "fa", "he", "ur", "ps", "sd", "yi", "dv", "ug"}


class PublicationError(RuntimeError):
    """Expected validation or rendering failure."""


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise PublicationError(f"Missing configuration: {path}")
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PublicationError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(result, dict):
        raise PublicationError(f"JSON root must be an object: {path}")
    return result


def secure_path(root: Path, value: str) -> Path:
    """Resolve project-relative input paths without allowing ../ escape."""
    if not isinstance(value, str) or not value:
        raise PublicationError("Expected a non-empty relative path")
    p = (root / value).resolve()
    if not p.is_relative_to(root.resolve()):
        raise PublicationError(f"Input path escapes repository: {value}")
    return p


def canonical_direction(locale: str) -> str:
    return "rtl" if locale.split("-", 1)[0].lower() in RTL_BASE else "ltr"


def metadata_for(repo: Path, book_id: str, locale: str) -> tuple[dict, dict, dict, Path]:
    if not LANGUAGE_RE.fullmatch(locale):
        raise PublicationError("Invalid BCP 47 language tag (example: ar, en, pt-BR, zh-Hant)")
    config = read_json(repo / "11 - publishing" / "publication.json")
    book = next((b for b in config.get("books", []) if b.get("id") == book_id), None)
    if not book:
        raise PublicationError(f"Unknown book {book_id}. Available: {[b.get('id') for b in config.get('books', [])]}")
    if locale == config.get("source_locale", "ar"):
        chapters = secure_path(repo, book["source_chapters"])
    else:
        chapters = secure_path(repo, f"10 - translations/{locale}/book-{book_id}/chapters")
    edition_path = secure_path(repo, f"11 - publishing/editions/{locale}/book-{book_id}.json")
    edition = read_json(edition_path)
    if edition.get("locale") != locale:
        raise PublicationError(f"Edition locale does not match path: {edition_path}")
    direction = edition.get("direction", canonical_direction(locale))
    if direction not in {"rtl", "ltr"}:
        raise PublicationError(f"Invalid text direction: {direction}")
    edition["direction"] = direction
    return config, book, edition, chapters


def parse_chapter(path: Path) -> tuple[dict, str]:
    source = path.read_text(encoding="utf-8")
    match = FRONT_RE.match(source)
    if match is None:
        raise PublicationError(f"Missing YAML frontmatter: {path}")
    data = yaml.safe_load(match.group(1))
    if not isinstance(data, dict):
        raise PublicationError(f"Invalid YAML frontmatter: {path}")
    return data, source[match.end():]


def normalize_markdown(content: str) -> str:
    """Remove editorial syntax from build output, without touching source."""
    content = re.sub(r"%%.*?%%", "", content, flags=re.DOTALL)
    content = WIKILINK_RE.sub(lambda m: m.group(1).split("|")[-1], content)
    return content.strip()


def available_chapters(chapters_dir: Path) -> list[Path]:
    if not chapters_dir.is_dir():
        return []
    # Numeric filename prefixes force stable alignment across translations.
    return sorted((p for p in chapters_dir.glob("[0-9]*.md") if p.is_file()), key=lambda p: p.name)


def manuscript_digest(chapters_dir: Path) -> str:
    """Fingerprint Arabic canonical source, excluding editorial infrastructure."""
    digest = hashlib.sha256()
    for chapter in available_chapters(chapters_dir):
        digest.update(chapter.name.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(chapter.read_bytes())
        digest.update(b"\x00")
    return digest.hexdigest()


def approved_assets(repo: Path, edition: dict, issues: list[str], warnings: list[str]) -> dict:
    result = {}
    for asset in edition.get("assets", []):
        asset_id = asset.get("id", "unspecified")
        try:
            path = secure_path(repo, asset.get("path", ""))
        except PublicationError as exc:
            issues.append(f"Asset {asset_id}: {exc}")
            continue
        if asset.get("status") != "approved":
            issues.append(f"Asset {asset_id} is not approved")
        if not path.is_file():
            issues.append(f"Asset {asset_id} missing binary: {path}")
            continue
        try:
            with Image.open(path) as img:
                width, height = img.size
                if asset.get("requires_alpha") and img.mode not in {"RGBA", "LA", "P"}:
                    issues.append(f"Asset {asset_id} requires alpha channel")
                if asset.get("requires_alpha") and img.mode == "P" and "transparency" not in img.info:
                    issues.append(f"Asset {asset_id} requires real transparency")
                print_width = asset.get("print_width_mm")
                if print_width:
                    ppi = width * 25.4 / float(print_width)
                    minimum = int(edition.get("minimum_print_ppi", 300))
                    if ppi < minimum:
                        issues.append(f"Asset {asset_id} has {ppi:.0f} effective PPI (min {minimum})")
                result[asset_id] = {"path": str(path), "size": [width, height]}
        except (OSError, ValueError) as exc:
            issues.append(f"Asset {asset_id} unreadable: {exc}")
    if not edition.get("assets"):
        warnings.append("No inserted illustrations declared; text-only publication is supported")
    return result


def preflight(repo: Path, book_id: str, locale: str, mode: str, formats: set[str]) -> dict:
    config, book, edition, chapters_dir = metadata_for(repo, book_id, locale)
    issues: list[str] = []
    warnings: list[str] = []
    chapter_items: list[dict] = []
    files = available_chapters(chapters_dir)
    expected = int(book.get("expected_chapters", 0))
    if expected <= 0:
        issues.append("expected_chapters must be a positive integer")
    if len(files) != expected:
        issues.append(f"Expected {expected} chapters, found {len(files)} for {locale}")
    if not files:
        issues.append("No manuscript chapters found")
    source_dir = secure_path(repo, book["source_chapters"])
    canonical_digest = manuscript_digest(source_dir)
    numeric_ids: list[int] = []
    for file in files:
        try:
            frontmatter, body = parse_chapter(file)
        except PublicationError as exc:
            issues.append(str(exc))
            continue
        match = re.match(r"^(\d+)", file.stem)
        number = int(match.group(1)) if match else 0
        numeric_ids.append(number)
        if frontmatter.get("chapter") != number:
            issues.append(f"{file.name}: chapter number mismatch (frontmatter vs filename)")
        if frontmatter.get("book") not in {None, int(book_id)}:
            issues.append(f"{file.name}: wrong book ID in frontmatter")
        state = frontmatter.get("status", "draft")
        if state not in READABLE:
            issues.append(f"{file.name}: status '{state}' is not preview-ready")
        elif mode == "release" and state not in APPROVED:
            issues.append(f"{file.name}: release requires approved/locked chapter")
        if not body.strip():
            issues.append(f"{file.name}: chapter has no prose")
        if CALLOUT_RE.search(body) or PLACEHOLDER_RE.search(body):
            issues.append(f"{file.name}: editorial callout or placeholder remains")
        if re.search(r"(?m)!\[[^\]]*\]\([^):]+\)", body):
            issues.append(f"{file.name}: inline local images must be registered as approved assets")
        chapter_items.append({"file": str(file.relative_to(repo)), "number": number,
                              "title": str(frontmatter.get("title", "")), "status": state,
                              "sha256": hashlib.sha256(file.read_bytes()).hexdigest()})
    if numeric_ids != list(range(1, len(numeric_ids) + 1)):
        issues.append("Chapter numbers must be consecutive starting at 1")
    if len(numeric_ids) != len(set(numeric_ids)):
        issues.append("Duplicate chapter numbers")
    if not edition.get("title"):
        issues.append("Edition title is required")
    if not edition.get("author"):
        (issues if mode == "release" else warnings).append("Missing author or explicit pen name")
    if edition.get("status") not in {"draft", "review", "approved"}:
        issues.append("Edition status must be draft, review or approved")
    if mode == "release":
        if edition.get("status") != "approved":
            issues.append("Edition must be editor-approved for RELEASE")
        if not re.fullmatch(r"\d+\.\d+\.\d+", str(edition.get("version", ""))):
            issues.append("Release edition needs a semantic version (for example 1.0.0)")
        if edition.get("rights_confirmed") is not True:
            issues.append("Rights approval is required for RELEASE")
        if not edition.get("copyright_holder") or not edition.get("copyright_year"):
            issues.append("Copyright holder and year required for RELEASE")
        if locale != config.get("source_locale", "ar"):
            if edition.get("translation_review") != "approved":
                issues.append("Human literary translation review required for RELEASE")
            if not edition.get("translator") or not edition.get("proofreader"):
                issues.append("Translation needs named translator and literary proofreader")
            if edition.get("source_revision") != config.get("source_revision"):
                issues.append("Translation is stale relative to approved source_revision")
            if edition.get("source_digest") != canonical_digest:
                issues.append("Translation is stale relative to exact canonical Arabic manuscript digest")
    cover = edition.get("cover_image")
    if cover:
        if mode == "release" and edition.get("cover_status") != "approved":
            issues.append("Retail cover must be explicitly approved for RELEASE")
        try:
            cover_path = secure_path(repo, cover)
            if not cover_path.is_file():
                issues.append(f"Cover image missing: {cover}")
        except PublicationError as exc:
            issues.append(str(exc))
    elif "epub" in formats:
        (issues if mode == "release" else warnings).append("No approved retail cover image for EPUB")
    if mode == "release" and "print-pdf" in formats:
        printer = edition.get("printer", {})
        if not printer.get("vendor") or not printer.get("specification_id") or printer.get("proof_approved") is not True:
            issues.append("Print release requires named printer, specification_id and approved physical proof")
    assets = approved_assets(repo, edition, issues, warnings)
    report = {
        "engine": VERSION,
        "mode": mode,
        "series": config.get("series_id"), "book": book_id, "language": locale,
        "direction": edition["direction"],
        "manuscript_dir": str(chapters_dir.relative_to(repo)) if chapters_dir.is_relative_to(repo) else str(chapters_dir),
        "expected_chapters": expected, "actual_chapters": len(files),
        "chapters": chapter_items, "approved_assets": assets,
        "errors": issues, "warnings": warnings, "ok": len(issues) == 0,
        "source_revision": config.get("source_revision"),
        "canonical_source_sha256": canonical_digest,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    return report


def _run(command: list[str], *, stdin: str | None = None, cwd: Path | None = None) -> str:
    try:
        p = subprocess.run(command, input=stdin, cwd=cwd, text=True, encoding="utf-8",
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    except FileNotFoundError as exc:
        raise PublicationError(f"Tool not installed: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        raise PublicationError(f"Tool failed: {' '.join(command)}\n{exc.stderr[-4000:]}") from exc
    return p.stdout


def _paragraph_html(md: str) -> str:
    return _run(["pandoc", "-f", "markdown", "-t", "html5", "--wrap=none"], stdin=md)


def _css(config: dict, edition: dict, *, printing: bool) -> str:
    layout = config.get("layout", {})
    rtl = edition.get("direction") == "rtl"
    font = layout.get("font_rtl", "Amiri") if rtl else layout.get("font_ltr", "Noto Serif")
    size = float(layout.get("body_size_pt", 11.5))
    line = float(layout.get("line_height", 1.7))
    base = f"""
    html {{ direction: {edition['direction']}; }}
    body {{ font-family: '{font}', serif; font-size: {size}pt; line-height: {line}; color: #141414; }}
    p {{ text-align: justify; widows: 2; orphans: 2; margin: 0 0 0.95em; }}
    h1 {{ font-size: 19pt; font-weight: bold; margin: 0 0 2em; text-align: center; break-before: page; break-after: avoid; }}
    h2 {{ font-size: 15pt; break-after: avoid; }}
    section.title-page {{ text-align: center; break-after: page; padding-top: 24%; }}
    section.title-page h1 {{ break-before: auto; font-size: 25pt; margin-bottom: 0.8em; }}
    section.title-page p {{ text-align: center; }}
    .preview-mark {{ font-size: 9pt; color: #766; padding-top: 3em; letter-spacing: 1px; }}
    blockquote {{ border-inline-start: 2px solid #b7a890; padding-inline-start: 12px; }}
    img {{ max-width: 100%; }}
    """
    if not printing:
        return base + "@page { size: A4; margin: 24mm 22mm; }"
    width = float(layout.get("trim_width_mm", 148))
    height = float(layout.get("trim_height_mm", 210))
    top = float(layout.get("top_mm", 18))
    bottom = float(layout.get("bottom_mm", 20))
    inside = float(layout.get("inside_mm", 22))
    outside = float(layout.get("outside_mm", 16))
    # Pages are mirrored. Binding direction affects chapter recto, not spine geometry.
    left_margin = f"{inside}mm {outside}mm" if not rtl else f"{outside}mm {inside}mm"
    right_margin = f"{outside}mm {inside}mm" if not rtl else f"{inside}mm {outside}mm"
    start = "left" if rtl else "right"
    return base + f"""
    @page {{ size: {width}mm {height}mm; margin-top: {top}mm; margin-bottom: {bottom}mm; }}
    @page:left {{ margin-left: {left_margin.split()[0]}; margin-right: {left_margin.split()[1]};
      @bottom-left {{ content: counter(page); font-size: 9pt; }} }}
    @page:right {{ margin-left: {right_margin.split()[0]}; margin-right: {right_margin.split()[1]};
      @bottom-right {{ content: counter(page); font-size: 9pt; }} }}
    main h1 {{ break-before: {start}; }}
    """


def _edition_text(repo: Path, book: dict, chapters_dir: Path) -> str:
    result = []
    for path in available_chapters(chapters_dir):
        data, body = parse_chapter(path)
        cleaned = normalize_markdown(body)
        if not re.match(r"^#\s+", cleaned):
            cleaned = f"# {data.get('title', path.stem)}\n\n" + cleaned
        result.append(cleaned)
    return "\n\n".join(result) + "\n"


def _enforce_epub_direction(epub_path: Path, direction: str) -> None:
    """Set EPUB page progression and XHTML direction explicitly for RTL readers."""
    import io
    import xml.etree.ElementTree as ET

    if direction != "rtl":
        return
    temp = epub_path.with_suffix(".repacked.epub")
    with zipfile.ZipFile(epub_path, "r") as original, zipfile.ZipFile(temp, "w") as target:
        data = {name: original.read(name) for name in original.namelist()}
        if data.get("mimetype") != b"application/epub+zip":
            raise PublicationError("Invalid EPUB MIME type")
        target.writestr("mimetype", data["mimetype"], compress_type=zipfile.ZIP_STORED)
        for name, content in data.items():
            if name == "mimetype":
                continue
            if name.endswith(".opf"):
                if b"page-progression-direction=" not in content:
                    content = re.sub(rb"<spine\b", b'<spine page-progression-direction="rtl"', content, count=1)
            elif name.endswith((".xhtml", ".html")):
                if b'dir="rtl"' not in content[:700]:
                    content = re.sub(rb"<html\b", b'<html dir="rtl"', content, count=1)
            target.writestr(name, content, compress_type=zipfile.ZIP_DEFLATED)
    temp.replace(epub_path)
    with zipfile.ZipFile(epub_path) as archive:
        if archive.infolist()[0].filename != "mimetype" or archive.infolist()[0].compress_type != zipfile.ZIP_STORED:
            raise PublicationError("EPUB must begin with an uncompressed mimetype entry")
        if not any(b'page-progression-direction="rtl"' in archive.read(n) for n in archive.namelist() if n.endswith('.opf')):
            raise PublicationError("EPUB reading direction verification failed")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _build(repo: Path, book_id: str, locale: str, mode: str, formats: set[str], out: Path) -> dict:
    report = preflight(repo, book_id, locale, mode, formats)
    out.mkdir(parents=True, exist_ok=True)
    (out / "preflight.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if not report["ok"]:
        raise PublicationError("Build blocked by preflight errors; see preflight.json")
    config, book, edition, chapters_dir = metadata_for(repo, book_id, locale)
    clean_md = _edition_text(repo, book, chapters_dir)
    source_md = clean_md
    if mode != "release":
        source_md = f"> **{mode.upper()} — NOT FOR SALE**\n\n" + clean_md
    manuscript_path = out / "normalized-book.md"
    manuscript_path.write_text(source_md, encoding="utf-8")
    title = str(edition["title"])
    author = str(edition.get("author", ""))
    direction = str(edition["direction"])
    cover = edition.get("cover_image")
    rendered = _paragraph_html(clean_md)
    edition_label = (f'<p class="preview-mark">{mode.upper()} — NOT FOR SALE</p>' if mode != "release" else "")
    html_content = f'''<!doctype html><html lang="{html.escape(locale)}" dir="{direction}"><head>
<meta charset="utf-8"><title>{html.escape(title)}</title><style>{_css(config, edition, printing=False)}</style>
</head><body><section class="title-page"><h1>{html.escape(title)}</h1><p>{html.escape(author)}</p>{edition_label}</section>
<main>{rendered}</main></body></html>'''
    print_content = html_content.replace(_css(config, edition, printing=False), _css(config, edition, printing=True))
    files = []
    if "html" in formats:
        (out / "Book-Preview.html").write_text(html_content, encoding="utf-8")
        files.append("Book-Preview.html")
    if "epub" in formats:
        cmd = ["pandoc", str(manuscript_path), "-f", "markdown", "-t", "epub3", "-o", str(out / "Book.epub"),
               "--toc", "--metadata", f"title={title}", "--metadata", f"lang={locale}",
               "--metadata", f"dir={direction}", "--metadata", f"author={author}"]
        if cover:
            cmd.extend(["--epub-cover-image", str(secure_path(repo, cover))])
        with tempfile.TemporaryDirectory() as tmp:
            ebook_css = Path(tmp) / "ebook.css"
            ebook_css.write_text(f"html, body {{ direction: {direction}; }} body {{ font-family: serif; }} p {{ text-align: start; }}", encoding="utf-8")
            cmd.extend(["--css", str(ebook_css)])
            _run(cmd)
        _enforce_epub_direction(out / "Book.epub", direction)
        with zipfile.ZipFile(out / "Book.epub") as archive:
            if archive.read("mimetype") != b"application/epub+zip":
                raise PublicationError("EPUB failed MIME validation")
        files.append("Book.epub")
    if "docx" in formats:
        _run(["pandoc", str(manuscript_path), "-f", "markdown", "-o", str(out / "Book.docx"),
              "--metadata", f"lang={locale}", "--metadata", f"title={title}"])
        files.append("Book.docx")
    if "digital-pdf" in formats or "print-pdf" in formats:
        from weasyprint import HTML
        if "digital-pdf" in formats:
            HTML(string=html_content, base_url=str(repo)).write_pdf(str(out / "Book-Digital.pdf"))
            files.append("Book-Digital.pdf")
        if "print-pdf" in formats:
            HTML(string=print_content, base_url=str(repo)).write_pdf(str(out / "Book-Print-Interior.pdf"))
            files.append("Book-Print-Interior.pdf")
    def tool_version(command: list[str]) -> str:
        try:
            return _run(command).splitlines()[0].strip()
        except PublicationError:
            return "unknown"

    try:
        commit = _run(["git", "rev-parse", "HEAD"], cwd=repo).strip()
    except PublicationError:
        commit = "unversioned-local-source"
    manifest = {"book": book_id, "series": config.get("series_id"), "locale": locale,
                "mode": mode, "engine": VERSION, "source_revision": config.get("source_revision"),
                "canonical_source_sha256": report["canonical_source_sha256"],
                "config_sha256": _sha256(repo / "11 - publishing" / "publication.json"),
                "source_commit": commit, "tool_versions": {"pandoc": tool_version(["pandoc", "--version"])},
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "files": [{"name": f, "sha256": _sha256(out / f), "bytes": (out / f).stat().st_size} for f in files],
                "rights_confirmed": edition.get("rights_confirmed", False),
                "ready_for_sale": mode == "release"}
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "SHA256SUMS").write_text("".join(f"{item['sha256']}  {item['name']}\n" for item in manifest["files"]), encoding="utf-8")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(prog="awwal-publish", description="Arabic-first multilingual novel publishing")
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("validate", "build"):
        p = sub.add_parser(action)
        p.add_argument("--repo", type=Path, default=Path.cwd())
        p.add_argument("--book", default="01")
        p.add_argument("--lang", default="ar")
        p.add_argument("--mode", choices=["preview", "proof", "release"], default="preview")
        p.add_argument("--formats", default="epub,digital-pdf,print-pdf,docx,html")
        if action == "build":
            p.add_argument("--out", type=Path)
    spine = sub.add_parser("spine")
    spine.add_argument("--pages", type=int, required=True)
    spine.add_argument("--sheet-thickness-mm", type=float, required=True)
    spine.add_argument("--trim-width-mm", type=float, default=148)
    spine.add_argument("--trim-height-mm", type=float, default=210)
    spine.add_argument("--bleed-mm", type=float, default=3)
    args = parser.parse_args()
    try:
        if args.action == "spine":
            if args.pages <= 0 or args.sheet_thickness_mm <= 0:
                raise PublicationError("Positive final pages and printer-provided sheet thickness are required")
            width = ((args.pages + 1) // 2) * args.sheet_thickness_mm
            print(json.dumps({"spine_mm": round(width, 3),
                              "cover_spread_width_mm": round(2 * args.trim_width_mm + width + 2 * args.bleed_mm, 3),
                              "cover_spread_height_mm": round(args.trim_height_mm + 2 * args.bleed_mm, 3),
                              "warning": "Assumes sheet thickness per physical sheet (2 pages). Confirm printer-specific formula and stock."}, indent=2))
            return
        repo = args.repo.resolve()
        formats = {v.strip() for v in args.formats.split(",") if v.strip()}
        unknown = formats - FORMATS
        if unknown or not formats:
            raise PublicationError(f"Unsupported formats: {unknown or 'empty set'}")
        if args.lang == "all":
            if getattr(args, "out", None):
                raise PublicationError("Custom --out is only supported for a single language")
            editions = (repo / "11 - publishing" / "editions").glob(f"*/book-{args.book}.json")
            languages = sorted({e.parent.name for e in editions})
            if not languages:
                raise PublicationError(f"No editions found for book {args.book}")
        else:
            languages = [args.lang]
        results = []
        errors = 0
        for language in languages:
            try:
                if args.action == "validate":
                    report = preflight(repo, args.book, language, args.mode, formats)
                    results.append(report)
                    errors += int(not report["ok"])
                else:
                    if getattr(args, "out", None):
                        out = args.out
                    else:
                        _, _, edition, _ = metadata_for(repo, args.book, language)
                        out = repo / "11 - publishing" / "dist" / args.book / language / args.mode
                        if args.mode == "release":
                            out = out / ("v" + str(edition.get("version", "0.0.0")))
                    result = _build(repo, args.book, language, args.mode, formats, out.resolve())
                    results.append(result)
            except PublicationError as exc:
                errors += 1
                results.append({"language": language, "error": str(exc)})
        print(json.dumps(results if len(languages) > 1 else results[0], ensure_ascii=False, indent=2))
        if errors:
            sys.exit(2)
    except PublicationError as exc:
        print(f"Publishing error: {exc}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
